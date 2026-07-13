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

PER_TRADE_CAP_FRAC = 0.05
PER_TRADE_CAP_ABS = 50.0
DAILY_LOSS_HALT_FRAC = 0.05
CONSECUTIVE_LOSS_HALT = 6
MAX_POSITIONS_PER_REGION_DAY = 3
PRICE_BAND = (0.03, 0.97)
PAPER_BANKROLL = 1000.0

DB_PATH = str(BOT_DIR / "polymarket_bot.db")
HALT_FILE = str(BOT_DIR / "HALT")

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
