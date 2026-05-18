from datetime import datetime, timezone
from typing import Any

import httpx

from app.odds_client import OddsApiClient
from app.prematch import is_prematch_event
from app.redis_store import RedisStore


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


async def sync_prematch_to_redis(
    api: OddsApiClient,
    store: RedisStore,
    *,
    sport: str = "football",
    league: str | None = None,
    limit: int = 200,
    bookmakers: str | None = None,
    fetch_odds: bool = True,
    odds_max_events: int = 0,
) -> dict[str, Any]:
    """
    Pull prematch events (+ optional odds) from Odds-API and write to Redis.
    odds_max_events: 0 = fetch odds for all synced events (in batches of 10).
    """
    raw = await api.events(sport=sport, league=league, limit=limit)
    if not isinstance(raw, list):
        raise ValueError("Unexpected events payload from upstream")

    prematch = [e for e in raw if isinstance(e, dict) and is_prematch_event(e)]

    leagues = None
    try:
        leagues_data = await api.leagues(sport)
        if isinstance(leagues_data, list):
            await store.set_leagues(sport, leagues_data)
            leagues = leagues_data
    except httpx.HTTPError:
        pass

    bm = (bookmakers or "").strip() or (await api.selected_bookmakers_csv() or "")

    odds_by_id: dict[int, Any] = {}
    odds_errors: list[dict[str, Any]] = []

    if fetch_odds and bm and prematch:
        ids = [int(e["id"]) for e in prematch if e.get("id") is not None]
        if odds_max_events > 0:
            ids = ids[:odds_max_events]
        for i in range(0, len(ids), 10):
            chunk = ids[i : i + 10]
            try:
                multi = await api.odds_multi(
                    event_ids=",".join(str(x) for x in chunk),
                    bookmakers=bm,
                )
            except httpx.HTTPStatusError as e:
                odds_errors.append(_upstream_odds_error_detail(e))
                continue
            if isinstance(multi, list):
                for item in multi:
                    if isinstance(item, dict) and item.get("id") is not None:
                        payload = dict(item)
                        payload["_meta"] = {"bookmakers_requested": bm}
                        odds_by_id[int(item["id"])] = payload

    await store.replace_prematch(sport, prematch, odds_by_id)

    synced_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "sport": sport,
        "synced_at": synced_at,
        "bookmakers": bm or None,
        "event_count": len(prematch),
        "odds_count": len(odds_by_id),
        "league_filter": league,
        "limit": limit,
    }
    await store.set_meta(sport, meta)

    return {
        "ok": True,
        "meta": meta,
        "leagues_cached": leagues is not None,
        "odds_errors": odds_errors,
    }
