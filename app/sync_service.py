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


def parse_odds_multi(multi: Any) -> list[dict[str, Any]]:
    """Normalize /odds/multi response to a list of event odds objects."""
    if isinstance(multi, list):
        return [x for x in multi if isinstance(x, dict)]
    if isinstance(multi, dict):
        for key in ("data", "events", "results", "items"):
            inner = multi.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        out: list[dict[str, Any]] = []
        for k, v in multi.items():
            if not isinstance(v, dict):
                continue
            item = dict(v)
            if item.get("id") is None:
                try:
                    item["id"] = int(k)
                except (TypeError, ValueError):
                    pass
            if item.get("id") is not None:
                out.append(item)
        return out
    return []


async def resolve_bookmakers(
    api: OddsApiClient,
    bookmakers: str | None,
    *,
    sport: str,
    store: RedisStore | None = None,
) -> str:
    bm = (bookmakers or "").strip()
    if bm:
        return bm
    if store is not None:
        meta = await store.get_meta(sport)
        if meta and meta.get("bookmakers"):
            return str(meta["bookmakers"]).strip()
    return (await api.selected_bookmakers_csv() or "").strip()


async def fetch_odds_one(
    api: OddsApiClient,
    event_id: int,
    bookmakers: str,
) -> dict[str, Any]:
    data = await api.odds(event_id=event_id, bookmakers=bookmakers)
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected odds payload for event {event_id}")
    return {**data, "_meta": {"bookmakers_requested": bookmakers}}


async def fetch_odds_batch(
    api: OddsApiClient,
    event_ids: list[int],
    bookmakers: str,
) -> tuple[dict[int, Any], list[dict[str, Any]]]:
    """Fetch odds via /odds/multi, then /odds for any still missing."""
    odds_by_id: dict[int, Any] = {}
    errors: list[dict[str, Any]] = []

    for i in range(0, len(event_ids), 10):
        chunk = event_ids[i : i + 10]
        try:
            multi = await api.odds_multi(
                event_ids=",".join(str(x) for x in chunk),
                bookmakers=bookmakers,
            )
            for item in parse_odds_multi(multi):
                eid = item.get("id")
                if eid is not None:
                    payload = dict(item)
                    payload["_meta"] = {"bookmakers_requested": bookmakers}
                    odds_by_id[int(eid)] = payload
        except httpx.HTTPStatusError as e:
            errors.append(_upstream_odds_error_detail(e))

        for eid in chunk:
            if eid in odds_by_id:
                continue
            try:
                odds_by_id[eid] = await fetch_odds_one(api, eid, bookmakers)
            except httpx.HTTPStatusError as e:
                errors.append({**_upstream_odds_error_detail(e), "event_id": eid})

    return odds_by_id, errors


async def ensure_odds_in_redis(
    api: OddsApiClient,
    store: RedisStore,
    sport: str,
    event_id: int,
    bookmakers: str | None = None,
    *,
    force_refresh: bool = False,
) -> Any:
    """Read odds from Redis; on miss fetch upstream, write Redis, return."""
    if not force_refresh:
        cached = await store.get_odds(sport, event_id)
        if cached is not None:
            return cached

    ev = await store.get_event(sport, event_id)
    if ev is None:
        raise ValueError(f"赛事 {event_id} 不在 Redis，请先同步赛事列表")

    bm = await resolve_bookmakers(api, bookmakers, sport=sport, store=store)
    if not bm:
        raise ValueError(
            "未指定庄家：请在页面填写「同步用庄家」，或在 Odds-API 控制台选择默认庄家后重新同步"
        )

    try:
        data = await fetch_odds_one(api, event_id, bm)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(_upstream_odds_error_detail(e)) from e

    await store.set_odds(sport, event_id, data)
    await store.recount_odds_in_meta(sport)
    return data


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

    bm = await resolve_bookmakers(api, bookmakers, sport=sport, store=store)

    odds_by_id: dict[int, Any] = {}
    odds_errors: list[dict[str, Any]] = []

    if fetch_odds and bm and prematch:
        ids = [int(e["id"]) for e in prematch if e.get("id") is not None]
        if odds_max_events > 0:
            ids = ids[:odds_max_events]
        odds_by_id, odds_errors = await fetch_odds_batch(api, ids, bm)

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
    await store.recount_odds_in_meta(sport)
    meta = await store.get_meta(sport) or meta

    return {
        "ok": True,
        "meta": meta,
        "leagues_cached": leagues is not None,
        "odds_errors": odds_errors,
    }
