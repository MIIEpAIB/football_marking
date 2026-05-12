import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
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

    bm_csv = (bookmakers or "").strip() or (await c.selected_bookmakers_csv() or "")

    odds_map: dict[int, Any] = {}
    if bm_csv and prematch:
        ids = [int(e["id"]) for e in prematch if e.get("id") is not None][:10]
        if ids:
            try:
                multi = await c.odds_multi(
                    event_ids=",".join(str(i) for i in ids),
                    bookmakers=bm_csv,
                )
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status_code=502,
                    detail=_upstream_odds_error_detail(e),
                ) from e
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
        description="Comma-separated; 省略时使用 Odds-API 账户已选默认庄家",
    ),
):
    c = _c()
    bm = (bookmakers or "").strip()
    if not bm:
        bm = (await c.selected_bookmakers_csv() or "").strip()
    if not bm:
        raise HTTPException(
            400,
            detail={
                "message": "未指定庄家：请在页面填写「赔率预览庄家」（逗号分隔），或在 Odds-API 控制台选择默认庄家后再试。",
            },
        )
    try:
        data = await c.odds(event_id=event_id, bookmakers=bm)
        if isinstance(data, dict):
            data = {
                **data,
                "_meta": {"bookmakers_requested": bm},
            }
        return data
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=_upstream_odds_error_detail(e),
        ) from e


@app.get("/")
async def index_page():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0 = 监听所有网卡，局域网/公网可通过本机 IP 访问（需防火墙放行）
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host=host, port=port)
