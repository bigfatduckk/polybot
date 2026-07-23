import os
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BOT_DIR.parent

CITIES = {
    "London": {
        "lat": 51.505, "lon": -0.056,
        "station_name": "London City Airport Station",
        "timezone": "Europe/London",
    },
    "Seoul": {
        "lat": 37.46, "lon": 126.44,
        "station_name": "Incheon Intl Airport Station",
        "timezone": "Asia/Seoul",
    },
    "Wellington": {
        "lat": -41.29, "lon": 174.80,
        "station_name": "Wellington Intl Airport Station",
        "timezone": "Pacific/Auckland",
    },
    "Buenos Aires": {
        "lat": -34.82, "lon": -58.54,
        "station_name": "Minister Pistarini Intl Airport Station",
        "timezone": "America/Argentina/Buenos_Aires",
    },
    "Ankara": {
        "lat": 40.13, "lon": 32.99,
        "station_name": "Esenboğa Intl Airport Station",
        "timezone": "Europe/Istanbul",
    },
    "Toronto": {
        "lat": 43.68, "lon": -79.61,
        "station_name": "Toronto Pearson Intl Airport Station",
        "timezone": "America/Toronto",
    },
    "Hong Kong": {
        "lat": 22.32, "lon": 114.17,
        "station_name": "Hong Kong Observatory",
        "timezone": "Asia/Hong_Kong",
    },
    "Tokyo": {
        "lat": 35.55, "lon": 139.78,
        "station_name": "Tokyo Haneda Airport Station",
        "timezone": "Asia/Tokyo",
    },
}

MODELS = {
    "ecmwf_ifs025": 0.5,
    "gfs025": 0.25,
    "icon_seamless": 0.25,
}

MODEL_COL_SUFFIX = {
    "ecmwf_ifs025": "ecmwf_ifs025_ensemble",
    "gfs025": "ncep_gefs025",
    "icon_seamless": "icon_seamless_eps",
}

MIN_EDGE = 0.06
MAX_LEAD_HOURS = 48
CONSENSUS_MAX_DISAGREEMENT_C = 1.5
STATION_BIAS_MIN = 0.30

# Bot C OOD robustness gate (paper only; _C). Stateless hysteresis on the
# per-city residual series: stand down a city's new candidates when the newest
# residual's |r - bias_C| exceeds K*SIGMA_CALM; stay down until it falls back
# under REARM*SIGMA_CALM (REARM < K → hysteresis). SIGMA_CALM is a FROZEN
# calm-baseline scale (pooled de-meaned MAD × 1.4826, 1.0°C floor) set once at
# launch by backtest_variance_gate.py — NOT rolling, or a storm inflates its own
# threshold and the gate self-disarms. ≤10d history → heuristic; proper K tuning
# deferred to ≥90d. Typhoon run = smoke test ("does it trip?"), not validation.
SIGMA_CALM = 1.0       # °C; conservative floor — replace via backtest_variance_gate.py
VAR_GATE_K = 4.0       # trip threshold (× σ_calm)
VAR_GATE_REARM = 2.5   # re-arm threshold (× σ_calm)

PER_TRADE_CAP_FRAC = 0.05
PER_TRADE_CAP_ABS = 50.0
DAILY_LOSS_HALT_FRAC = 0.05
CONSECUTIVE_LOSS_HALT = 6
MAX_POSITIONS_PER_REGION_DAY = 3
PRICE_BAND = (0.03, 0.97)
PAPER_BANKROLL = 1000.0

# A/B/C instance switch. One codebase, config-driven — not a cp -r.
#   B = climatology blend (α linear-pool w/ ERA5 climatology); weather+maintain only.
#   C = paper-only OOD-robustness variant (winsorized-EWM bias + residual-z gate);
#       never run_live.py, never a wallet. Same weather scan as A.
BOT_INSTANCE = os.environ.get("BOT_INSTANCE", "A").upper()
_B = BOT_INSTANCE == "B"
_C = BOT_INSTANCE == "C"
INST_TAG = "B" if _B else ("C" if _C else "A")
DB_PATH = str(BOT_DIR / (
    "polymarket_bot_B.db" if _B
    else ("polymarket_bot_C.db" if _C else "polymarket_bot.db")))
HALT_FILE = str(BOT_DIR / ("HALT_B" if _B else ("HALT_C" if _C else "HALT")))

# Climatology blend (Bot B only). Linear pool: p=(1-α)*p_model + α*p_clim.
# p_clim = historical fraction of daily-highs (±CLIM_WINDOW_DAYS around the
# target calendar date, over CLIM_YEARS recent years) falling in each bucket.
# Recent-decade window embeds the warming trend (no detrend needed). α tunable
# during IS (07-19→07-25), frozen 07-26.
CLIM_ENABLED = _B
CLIM_ALPHA = 0.30
CLIM_WINDOW_DAYS = 7
CLIM_YEARS = (2016, 2025)
CLIM_HIST_PATH = str(BOT_DIR / "data" / "clim_hist.json")

# Snapshot retention. analyze.py reads market_mid from candidates (not
# snapshots), so old snapshots are dead weight past the settlement window.
# 14d covers Open-Meteo Archive's ~5d observed-high lag + settlement retry.
CULL_SNAP_DAYS = 14

TELEGRAM_TOKEN_ENV = "TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"


def tls_verify():
    return os.environ.get("POLYBOT_TLS_VERIFY", "true").lower() not in ("0", "false", "no")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

WEATHER_EVENT_TITLE_RE = r"highest temperature in (.+) on (.+)"
TAG_REFRESH_DAYS = 7
DAILY_PULSE_HOUR_HKT = 21

FLB_HORIZON_DAYS = (7, 90)
FLB_MIN_VOLUME = 5000.0
FLB_MIN_LIQUIDITY = 500.0
FLB_CALIB_PATH = str(BOT_DIR / "data" / "flb_calib.json")
WEATHER_CALIB_PATH = str(BOT_DIR / "data" / "weather_calib.json")
FLB_PRICE_BUCKET = 0.05
FLB_HORIZON_BUCKETS = [(0, 7), (7, 30), (30, 90), (90, 100000)]

ARB_MIN_GAP = 0.02
ARB_MIN_DEPTH = 50.0
ARB_MAX_OUTCOMES = 12
ARB_SCAN_TAGS = ["politics", "crypto"]

CROSSVENUE_MIN_GAP = 0.03
CROSSVENUE_TAGS = ["politics", "economics"]
KALSHI_BASE = "https://api.elections.kalshi.com"

USUD_TICKERS = ["SPY", "SPX", "DJIA", "NVDA", "TSLA"]
USUD_MIN_EDGE = 0.05
USUD_MIN_DEPTH = 500.0
USUD_RISK_FREE = 0.0
PAPER_BANKROLL_USUD = 1000.0

PAPER_BANKROLL_FLB = 1000.0
PAPER_BANKROLL_ARB = 1000.0

# ── LIVE TRADING (M4) ──────────────────────────────────────────────────────
# Separate process (run_live.py) reading paper candidates READ-ONLY. Paper
# crons never import live modules; live modules import paper modules only for
# pure/read-only helpers. All live state → polymarket_bot_live.db. The paper
# DBs (polymarket_bot.db / polymarket_bot_B.db) are never written by live.
LIVE_DB_PATH = str(BOT_DIR / "polymarket_bot_live.db")
HALT_LIVE_FILE = str(BOT_DIR / "HALT_LIVE")

LIVE_BANKROLL = 1000.0
LIVE_PER_TRADE_CAP_FRAC = 0.05
LIVE_PER_TRADE_CAP_ABS = 50.0
LIVE_DAILY_LOSS_HALT_FRAC = 0.10
LIVE_CONSECUTIVE_LOSS_HALT = 6
LIVE_MAX_OPEN_POSITIONS = 5
LIVE_MAX_POSITIONS_PER_REGION_DAY = 3
LIVE_MIN_EDGE = 0.08
LIVE_PRICE_BAND = (0.03, 0.97)
LIVE_SIGNAL_MAX_AGE_MIN = 45
LIVE_ORDER_STALE_MIN = 90
LIVE_MATIC_ALERT = 0.5
# Free-collateral floor (pUSD). Fires when the proxy balanceOf drops below
# this — capital is locked in open fills OR lost. Check open positions to tell.
LIVE_PUSD_ALERT = 50.0

# Env-var NAMES (values read at runtime on the VPS, never in repo):
LIVE_KEY_ENV = "POLY_PRIVATE_KEY"
LIVE_FUNDER_ENV = "POLY_FUNDER"
LIVE_SIG_TYPE_ENV = "POLY_SIG_TYPE"
LIVE_DRY_RUN_ENV = "LIVE_DRY_RUN"

# Polygon RPC (public; read-only gas/USDC balance checks). polygon-rpc.com
# 401s from the VPS ("API key disabled / tenant disabled", 2026-07-20); these
# two returned 135.6 POL for the EOA same-day. fetch_balances tries primary
# then fallbacks; any failure → (None, None) so check_balances skips the alert
# rather than false-positiving on 0.
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
POLYGON_RPC_FALLBACKS = ["https://polygon.drpc.org"]
# Polymarket pUSD collateral (proxy ERC-20, 6 decimals) — what the proxy
# actually holds. Confirmed 2026-07-21 via eth_call: balanceOf(0xc011a7…,
# 0xfd043) = 200.0; native USDC 0x3c499… and USDC.e 0x2791… return no code /
# empty on every RPC we have (publicnode, drpc, alchemy) — pUSD is the live one.
USDC_CONTRACT = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
USDC_DECIMALS = 6
