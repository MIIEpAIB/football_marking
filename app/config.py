import os

from dotenv import load_dotenv

load_dotenv()

ODDS_API_BASE = "https://api.odds-api.io/v3"


def get_api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing ODDS_API_KEY. Set it in environment or .env (see .env.example)."
        )
    return key
