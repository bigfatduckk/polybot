"""Live order executor. The ONLY module that imports py_clob_client_v2 and
touches the private key. py_clob_client_v2 is imported lazily inside functions
so a missing/old SDK never breaks config, live_engine, live_settle, or the
paper crons.

Dry-run is the default (LIVE_DRY_RUN absent or != "0"): create_order() signs
the order (proving key + structure work) but post_order() is never called, so
zero on-chain transactions. Go-live requires the user to write LIVE_DRY_RUN=0.
"""
import json
import os

import httpx

import live_engine as le
import markets
from config import (
    CLOB_BASE,
    GAMMA_BASE,
    LIVE_DRY_RUN_ENV,
    LIVE_FUNDER_ENV,
    LIVE_KEY_ENV,
    LIVE_MIN_EDGE,
    LIVE_SIG_TYPE_ENV,
    POLYGON_RPC,
    POLYGON_RPC_FALLBACKS,
    USDC_CONTRACT,
    USDC_DECIMALS,
    tls_verify,
)


class LiveKeyMissing(Exception):
    pass


def is_dry_run():
    """Dry-run defaults ON; go-live requires explicit LIVE_DRY_RUN=0."""
    return os.environ.get(LIVE_DRY_RUN_ENV, "1") != "0"


def _sig_type():
    try:
        return int(os.environ.get(LIVE_SIG_TYPE_ENV, "2"))
    except ValueError:
        return 2


def get_client():
    """Build a ClobClient from env. Raises LiveKeyMissing if key/funder absent."""
    key = os.environ.get(LIVE_KEY_ENV)
    funder = os.environ.get(LIVE_FUNDER_ENV)
    if not key:
        raise LiveKeyMissing("POLY_PRIVATE_KEY not set in env")
    if not funder:
        raise LiveKeyMissing("POLY_FUNDER not set in env")
    from py_clob_client_v2 import ClobClient
    client = ClobClient(
        host=CLOB_BASE, chain_id=137, key=key,
        signature_type=_sig_type(), funder=funder,
    )
    # Derive-first: GET the existing key on steady state; POST-create only as
    # first-run fallback. create_or_derive_api_key() POSTs-then-GETs, logging a
    # noisy 400 "key already exists" every tick after the first — derive-first
    # avoids it (the 400 was cosmetic, auth always succeeded).
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    return client


def resolve_no_token(market_id):
    """Gamma /markets?id= → clobTokenIds[1] (the NO token). Read-only HTTP."""
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": "MarcusVaultBot/1.0"},
                          verify=tls_verify()) as c:
            r = c.get(f"{GAMMA_BASE}/markets", params={"id": market_id})
            mkts = r.json() if r.status_code == 200 else []
    except Exception:
        return None
    if not mkts:
        return None
    try:
        tokens = json.loads(mkts[0].get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    return tokens[1] if len(tokens) >= 2 else None


def _exec_token_and_book(signal, client):
    """Return (exec_token_id, book_dict). Buy → YES book; sell → NO book."""
    if signal.side == "buy":
        token = signal.yes_token_id
    else:
        token = resolve_no_token(signal.market_id)
    if not token:
        return None, None
    book = markets.fetch_book(token)
    return token, book


def prepare_order(signal, client, live_conn):
    """Fresh book → walk → size → recheck edge at walked price.
    Returns a LiveOrderSpec ready to submit, or None (skip logged to live_ticks)."""
    token, book = _exec_token_and_book(signal, client)
    if not token or not book:
        le.log_tick(live_conn, "weather-live", "skip:no_book",
                     {"candidate_id": signal.candidate_id, "market_id": signal.market_id})
        return None
    (bids, asks, bb, ba, bs, asz, depth, tick, min_sz,
     neg, last) = markets._parse_book(book)
    if not asks:
        le.log_tick(live_conn, "weather-live", "skip:empty_book",
                     {"candidate_id": signal.candidate_id})
        return None
    # target_shares = cap / best ask (mirrors engine.scan_weather's walk target)
    best = asks[0]["price"] if asks else 1.0
    target = (le.LIVE_PER_TRADE_CAP_ABS / best) if best > 0 else 0.0
    avg, fillable = le.walk_book_fill(asks, target)
    if avg is None or fillable <= 0:
        le.log_tick(live_conn, "weather-live", "skip:no_depth",
                     {"candidate_id": signal.candidate_id})
        return None
    notional, shares, edge = le.size_signal(signal, walked_exec_price=avg)
    if notional is None:
        le.log_tick(live_conn, "weather-live", "skip:no_stake",
                     {"candidate_id": signal.candidate_id, "avg": avg})
        return None
    if edge < LIVE_MIN_EDGE:
        le.log_tick(live_conn, "weather-live", "skip:edge_below_min_at_exec",
                     {"candidate_id": signal.candidate_id, "edge_at_exec": edge})
        return None
    # never size up to a minimum; cap at fillable
    size = min(shares, fillable)
    size = float(int(size))           # CLOB shares are integer token units
    if size < max(min_sz, 1.0):
        le.log_tick(live_conn, "weather-live", "skip:below_clob_min",
                     {"candidate_id": signal.candidate_id, "size": size, "min": min_sz})
        return None
    # limit price = deepest consumed level (walk `size` shares down the book)
    rem = size
    deepest = asks[0]["price"]
    for lvl in asks:
        if rem <= 0:
            break
        deepest = lvl["price"]
        rem -= lvl["size"]
    tick_val = float(tick) if tick else _get_tick(client, token)
    limit = round(deepest / tick_val) * tick_val if tick_val > 0 else deepest
    notional = limit * size
    neg_risk = bool(neg) or signal.neg_risk or _get_neg_risk(client, token)
    return le.LiveOrderSpec(
        signal=signal, exec_token_id=token, exec_side="BUY",
        price=limit, size=size, notional=notional,
        edge_at_exec=edge, kelly_fraction=(notional / le.LIVE_BANKROLL) * 4.0,
    ), {"tick_size": str(tick_val), "neg_risk": neg_risk}


def _get_tick(client, token):
    try:
        s = client.get_tick_size(token)
        return float(s)
    except Exception:
        return 0.001


def _get_neg_risk(client, token):
    try:
        return bool(client.get_neg_risk(token))
    except Exception:
        return False


def submit(order_spec, options, client, live_conn, dry_run):
    """Sign the order always; post only if not dry_run. Stores a live_orders row."""
    from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions, OrderType
    from py_clob_client_v2.order_builder.constants import BUY
    args = OrderArgsV2(
        token_id=order_spec.exec_token_id, price=order_spec.price,
        size=order_spec.size, side=BUY,
    )
    opts = PartialCreateOrderOptions(
        tick_size=options["tick_size"], neg_risk=options["neg_risk"],
    )
    try:
        signed = client.create_order(args, opts)
    except Exception as e:
        le.log_tick(live_conn, "weather-live", "create_order_failed",
                     {"candidate_id": order_spec.signal.candidate_id, "error": str(e)[:300]})
        _store_order(live_conn, order_spec, options, status="rejected",
                     clob_id=None, raw={"error": str(e)[:500]})
        return "rejected", str(e)
    if dry_run:
        _store_order(live_conn, order_spec, options, status="dry_run",
                     clob_id=None, raw={"signed": "ok", "dry_run": True})
        return "dry_run", None
    try:
        resp = client.post_order(signed, OrderType.GTC)
    except Exception as e:
        le.log_tick(live_conn, "weather-live", "post_order_failed",
                     {"candidate_id": order_spec.signal.candidate_id, "error": str(e)[:300]})
        _store_order(live_conn, order_spec, options, status="rejected",
                     clob_id=None, raw={"error": str(e)[:500]})
        return "rejected", str(e)
    clob_id = ""
    try:
        if isinstance(resp, dict):
            clob_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
    except Exception:
        pass
    _store_order(live_conn, order_spec, options, status="posted",
                 clob_id=clob_id, raw={"resp": str(resp)[:500]})
    return "posted", clob_id


_LO_COLS = """ts, candidate_id, market_id, condition_id, exec_token_id,
  city, market_date, bucket_key, signal_side, exec_side, price, size, notional,
  edge_at_exec, kelly_fraction, neg_risk, dry_run, clob_order_id, status, raw_json"""
_LO_PH = ",".join(["?"] * 20)


def _store_order(live_conn, spec, options, status, clob_id, raw):
    sig = spec.signal
    live_conn.execute(
        f"INSERT INTO live_orders({_LO_COLS}) VALUES({_LO_PH})",
        (le._now_iso(), sig.candidate_id, sig.market_id, sig.condition_id,
         spec.exec_token_id, sig.city, sig.market_date, sig.bucket_key,
         sig.side, spec.exec_side, spec.price, spec.size, spec.notional,
         spec.edge_at_exec, spec.kelly_fraction, int(options["neg_risk"]),
         int(is_dry_run()), clob_id or "", status, json.dumps(raw, default=str)),
    )
    live_conn.commit()


# ── balance check (read-only RPC; live_settle calls this) ──────────────────
def fetch_balances(funder):
    """Raw JSON-RPC: native POL (eth_getBalance) + USDC balanceOf(funder).
    No SDK. Returns (usdc, matic) as floats, or (None, None) on any RPC/parse
    failure so callers skip the alert instead of false-positiving on 0. Tries
    POLYGON_RPC then each fallback. Results matched by id, not batch position
    (JSON-RPC batch order is not guaranteed)."""
    usdc_data = _erc20_balance_of_data(funder)
    payload = [{"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                "params": [funder, "latest"]},
               {"jsonrpc": "2.0", "id": 2, "method": "eth_call",
                "params": [{"to": USDC_CONTRACT, "data": usdc_data}, "latest"]}]
    # Alchemy free-tier if POLYGON_RPC_KEY set in .env (publicnode flaky for
    # eth_call; polygon-rpc.com/ankr 403 key-gated). Read at call time — config
    # import runs before load_dotenv, so a module-level os.environ.get would miss it.
    _rpc_key = os.environ.get("POLYGON_RPC_KEY", "")
    _rpcs = ([f"https://polygon-mainnet.g.alchemy.com/v2/{_rpc_key}"] if _rpc_key else []) + [POLYGON_RPC, *POLYGON_RPC_FALLBACKS]
    for rpc in _rpcs:
        try:
            r = httpx.post(rpc, json=payload, timeout=30,
                           headers={"User-Agent": "MarcusVaultBot/1.0"},
                           verify=tls_verify())
            if r.status_code != 200:
                continue
            out = r.json()
        except Exception:
            continue
        matic_hex = _rpc_result(out, 1)
        usdc_hex = _rpc_result(out, 2)
        if matic_hex is None or usdc_hex is None:
            continue
        return _hex_to_float(usdc_hex, USDC_DECIMALS), _hex_to_float(matic_hex, 18)
    return None, None


def _erc20_balance_of_data(funder):
    # balanceOf(address) selector = 0x70a08231; pad address to 32 bytes
    addr = funder[2:] if funder.startswith("0x") else funder
    addr = addr.rjust(64, "0")
    return "0x70a08231" + addr


def _rpc_result(batch, rpc_id):
    """Match a JSON-RPC batch response item by id. Returns the result hex
    string, or None if the batch isn't a list, the id is absent, or the item
    carries an error/missing result — so RPC failures surface as None instead
    of a silent 0."""
    if not isinstance(batch, list):
        return None
    for item in batch:
        if isinstance(item, dict) and item.get("id") == rpc_id:
            if "error" in item or "result" not in item:
                return None
            return item.get("result") or "0x0"
    return None


def _hex_to_float(hexval, decimals):
    try:
        return int(hexval, 16) / (10 ** decimals)
    except (TypeError, ValueError):
        return 0.0
