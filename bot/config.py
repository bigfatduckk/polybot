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

PER_TRADE_CAP_FRAC = 0.05
PER_TRADE_CAP_ABS = 50.0
DAILY_LOSS_HALT_FRAC = 0.05
CONSECUTIVE_LOSS_HALT = 6
MAX_POSITIONS_PER_REGION_DAY = 3
PRICE_BAND = (0.03, 0.97)
PAPER_BANKROLL = 1000.0

# A/B instance switch. BOT_INSTANCE=B forks the weather bot into a separate
# paper DB/HALT (Bot B = climatology blend; A = current baseline). One codebase,
# config-driven — not a cp -r. B crons weather+maintain only (weather A/B).
BOT_INSTANCE = os.environ.get("BOT_INSTANCE", "A").upper()
_B = BOT_INSTANCE == "B"
INST_TAG = "B" if _B else "A"
DB_PATH = str(BOT_DIR / ("polymarket_bot_B.db" if _B else "polymarket_bot.db"))
HALT_FILE = str(BOT_DIR / ("HALT_B" if _B else "HALT"))

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
