import os

from dotenv import load_dotenv

load_dotenv()

ODDS_API_BASE = "https://api.odds-api.io/v3"
DEFAULT_SPORT = "football"


def get_redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip()


def get_redis_prefix() -> str:
    return os.getenv("REDIS_PREFIX", "fb").strip() or "fb"


def get_sync_event_limit() -> int:
    return max(1, min(500, int(os.getenv("SYNC_EVENT_LIMIT", "200"))))


def get_sync_odds_max_events() -> int:
    """0 = fetch odds for all synced events."""
    return max(0, int(os.getenv("SYNC_ODDS_MAX_EVENTS", "0")))


def get_api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing ODDS_API_KEY. Set it in environment or .env (see .env.example)."
        )
    return key
