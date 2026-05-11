from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_api_key
from app.odds_client import OddsApiClient

client: OddsApiClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = OddsApiClient()
    try:
        yield
    finally:
        if client:
            await client.aclose()
            client = None


app = FastAPI(title="Odds-API 早盘赛事", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _c() -> OddsApiClient:
    if client is None:
        raise HTTPException(503, "Service not ready")
    return client


def _is_prematch_event(ev: dict[str, Any]) -> bool:
    st = (ev.get("status") or "").lower()
    if st == "live":
        return False
    if st == "settled":
        return False
    raw = ev.get("date")
    if not raw:
        return True
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


@app.get("/api/health")
async def health():
    try:
        get_api_key()
        return {"ok": True, "api_key_configured": True}
    except RuntimeError:
        return {"ok": True, "api_key_configured": False}


@app.get("/api/sports")
async def api_sports():
    return await _c().sports()


@app.get("/api/bookmakers")
async def api_bookmakers():
    return await _c().bookmakers()


@app.get("/api/bookmakers/selected")
async def api_bookmakers_selected():
    return await _c().bookmakers_selected()


@app.get("/api/leagues")
async def api_leagues(sport: str = Query("football", description="Sport slug")):
    return await _c().leagues(sport)


@app.get("/api/events/prematch")
async def api_prematch_events(
    sport: str = Query("football"),
    league: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    bookmakers: str | None = Query(
        None,
        description="Comma bookmakers for optional odds snapshot via /odds/multi",
    ),
):
    """
    早盘：赛前未开赛赛事（排除 live / settled，且开赛时间不早于当前 UTC）。
    """
    c = _c()
    raw = await c.events(sport=sport, league=league, limit=limit)
    if not isinstance(raw, list):
        raise HTTPException(502, "Unexpected events payload")
    prematch = [e for e in raw if isinstance(e, dict) and _is_prematch_event(e)]

    odds_map: dict[int, Any] = {}
    if bookmakers and prematch:
        ids = [int(e["id"]) for e in prematch if e.get("id") is not None][:10]
        if ids:
            multi = await c.odds_multi(
                event_ids=",".join(str(i) for i in ids),
                bookmakers=bookmakers,
            )
            if isinstance(multi, list):
                for item in multi:
                    if isinstance(item, dict) and item.get("id") is not None:
                        odds_map[int(item["id"])] = item

    return {"events": prematch, "odds_preview": odds_map}


@app.get("/api/events/{event_id}")
async def api_event(event_id: int):
    return await _c().event_by_id(event_id)


@app.get("/api/odds/{event_id}")
async def api_odds(
    event_id: int,
    bookmakers: str | None = Query(
        None,
        description="Comma-separated; omit to use account default selection if API allows",
    ),
):
    return await _c().odds(event_id=event_id, bookmakers=bookmakers)


@app.get("/")
async def index_page():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
