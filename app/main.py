import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import (
    DEFAULT_SPORT,
    get_api_key,
    get_sync_event_limit,
    get_sync_odds_max_events,
)
from app.odds_client import OddsApiClient
from app.redis_store import RedisStore
from app.sync_service import ensure_odds_in_redis, sync_prematch_to_redis

api_client: OddsApiClient | None = None
redis_store: RedisStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global api_client, redis_store
    api_client = OddsApiClient()
    redis_store = RedisStore()
    try:
        await redis_store.connect()
    except Exception as e:
        redis_store = None
        app.state.redis_error = str(e)
    try:
        yield
    finally:
        if api_client:
            await api_client.aclose()
            api_client = None
        if redis_store:
            await redis_store.close()
            redis_store = None


app = FastAPI(title="Odds-API 早盘赛事 (Redis)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _api() -> OddsApiClient:
    if api_client is None:
        raise HTTPException(503, "Service not ready")
    return api_client


def _redis() -> RedisStore:
    if redis_store is None:
        err = getattr(app.state, "redis_error", "Redis unavailable")
        raise HTTPException(503, detail={"message": "Redis 未连接", "error": err})
    return redis_store


@app.get("/api/health")
async def health():
    api_ok = True
    try:
        get_api_key()
    except RuntimeError:
        api_ok = False
    redis_ok = False
    meta = None
    if redis_store is not None:
        redis_ok = await redis_store.ping()
        if redis_ok:
            meta = await redis_store.get_meta(DEFAULT_SPORT)
    return {
        "ok": True,
        "api_key_configured": api_ok,
        "redis_connected": redis_ok,
        "cache_meta": meta,
    }


@app.get("/api/cache/meta")
async def api_cache_meta(sport: str = Query(DEFAULT_SPORT)):
    meta = await _redis().get_meta(sport)
    if not meta:
        raise HTTPException(404, detail={"message": "尚无缓存，请先点击「同步到 Redis」"})
    return meta


@app.post("/api/sync/prematch")
async def api_sync_prematch(
    sport: str = Query(DEFAULT_SPORT),
    league: str | None = Query(None),
    limit: int = Query(None, ge=1, le=500),
    bookmakers: str | None = Query(None),
    fetch_odds: bool = Query(True),
):
    """从 Odds-API 拉取早盘并写入 Redis（下注前执行此步骤）。"""
    try:
        get_api_key()
    except RuntimeError as e:
        raise HTTPException(400, detail={"message": str(e)}) from e

    lim = limit if limit is not None else get_sync_event_limit()
    try:
        result = await sync_prematch_to_redis(
            _api(),
            _redis(),
            sport=sport,
            league=league,
            limit=lim,
            bookmakers=bookmakers,
            fetch_odds=fetch_odds,
            odds_max_events=get_sync_odds_max_events(),
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, detail=_upstream_odds_error_detail(e)) from e
    except ValueError as e:
        raise HTTPException(502, detail={"message": str(e)}) from e
    return result


@app.get("/api/leagues")
async def api_leagues(sport: str = Query(DEFAULT_SPORT)):
    cached = await _redis().get_leagues(sport)
    if cached is not None:
        return cached
    raise HTTPException(
        404,
        detail={"message": "联赛列表未缓存，请先执行「同步到 Redis」"},
    )


@app.get("/api/events/prematch")
async def api_prematch_events(
    sport: str = Query(DEFAULT_SPORT),
    league: str | None = Query(None),
):
    """页面展示：仅从 Redis 读取。"""
    events, odds_map = await _redis().list_prematch(
        sport,
        league_slug=league or None,
    )
    meta = await _redis().get_meta(sport)
    if not events and not meta:
        raise HTTPException(
            404,
            detail={"message": "Redis 中暂无早盘数据，请先点击「同步到 Redis」"},
        )
    return {
        "events": events,
        "odds_preview": odds_map,
        "meta": meta,
        "source": "redis",
    }


@app.get("/api/events/{event_id}")
async def api_event(event_id: int, sport: str = Query(DEFAULT_SPORT)):
    ev = await _redis().get_event(sport, event_id)
    if not ev:
        raise HTTPException(404, detail={"message": f"赛事 {event_id} 不在 Redis 缓存中"})
    return ev


@app.get("/api/odds/{event_id}")
async def api_odds(
    event_id: int,
    sport: str = Query(DEFAULT_SPORT),
    bookmakers: str | None = Query(None),
    refresh: bool = Query(False, description="强制从 Odds-API 拉取并写回 Redis"),
):
    """优先读 Redis；未命中则拉取上游并写入 Redis 后返回。"""
    store = _redis()
    if not refresh:
        cached = await store.get_odds(sport, event_id)
        if cached is not None:
            return cached

    try:
        get_api_key()
    except RuntimeError as e:
        raise HTTPException(400, detail={"message": str(e)}) from e

    try:
        return await ensure_odds_in_redis(
            _api(),
            store,
            sport,
            event_id,
            bookmakers,
            force_refresh=refresh,
        )
    except ValueError as e:
        raise HTTPException(404, detail={"message": str(e)}) from e
    except RuntimeError as e:
        err = e.args[0] if e.args else {"message": str(e)}
        if isinstance(err, dict):
            raise HTTPException(502, detail=err) from e
        raise HTTPException(502, detail={"message": str(e)}) from e


@app.post("/api/sync/odds/{event_id}")
async def api_sync_odds_one(
    event_id: int,
    sport: str = Query(DEFAULT_SPORT),
    bookmakers: str | None = Query(None),
):
    """单场赔率：从 Odds-API 拉取并写入 Redis。"""
    try:
        get_api_key()
    except RuntimeError as e:
        raise HTTPException(400, detail={"message": str(e)}) from e
    try:
        data = await ensure_odds_in_redis(
            _api(),
            _redis(),
            sport,
            event_id,
            bookmakers,
            force_refresh=True,
        )
        return {"ok": True, "odds": data}
    except ValueError as e:
        raise HTTPException(404, detail={"message": str(e)}) from e
    except RuntimeError as e:
        err = e.args[0] if e.args else {"message": str(e)}
        if isinstance(err, dict):
            raise HTTPException(502, detail=err) from e
        raise HTTPException(502, detail={"message": str(e)}) from e


# --- 直连上游（调试用，页面默认不用） ---


@app.get("/api/live/sports")
async def api_live_sports():
    return await _api().sports()


@app.get("/api/live/bookmakers/selected")
async def api_live_bookmakers_selected():
    return await _api().bookmakers_selected()


def _upstream_odds_error_detail(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    try:
        body: Any = exc.response.json()
    except Exception:
        body = exc.response.text[:8000]
    return {
        "message": "上游赔率接口返回错误",
        "status_code": exc.response.status_code,
        "body": body,
    }


@app.get("/")
async def index_page():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host=host, port=port)
