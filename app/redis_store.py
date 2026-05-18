import json
from typing import Any

from redis.asyncio import Redis

from app.config import get_redis_prefix, get_redis_url


class RedisStore:
    def __init__(self) -> None:
        self._redis: Redis | None = None

    async def connect(self) -> None:
        self._redis = Redis.from_url(get_redis_url(), decode_responses=True)
        await self._redis.ping()

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    def _r(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("Redis not connected")
        return self._redis

    def _sport_key(self, sport: str, *parts: str) -> str:
        p = get_redis_prefix()
        return ":".join([p, sport, *parts])

    async def ping(self) -> bool:
        try:
            return bool(await self._r().ping())
        except Exception:
            return False

    async def get_meta(self, sport: str) -> dict[str, Any] | None:
        raw = await self._r().get(self._sport_key(sport, "meta"))
        if not raw:
            return None
        return json.loads(raw)

    async def set_meta(self, sport: str, meta: dict[str, Any]) -> None:
        await self._r().set(
            self._sport_key(sport, "meta"),
            json.dumps(meta, ensure_ascii=False),
        )

    async def get_leagues(self, sport: str) -> list[Any] | None:
        raw = await self._r().get(self._sport_key(sport, "leagues"))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, list) else None

    async def set_leagues(self, sport: str, leagues: list[Any]) -> None:
        await self._r().set(
            self._sport_key(sport, "leagues"),
            json.dumps(leagues, ensure_ascii=False),
        )

    async def replace_prematch(
        self,
        sport: str,
        events: list[dict[str, Any]],
        odds_by_id: dict[int, Any],
    ) -> None:
        r = self._r()
        pipe = r.pipeline()
        ids_key = self._sport_key(sport, "event_ids")
        old_ids = await r.lrange(ids_key, 0, -1)
        for eid in old_ids:
            pipe.delete(self._sport_key(sport, "event", eid))
            pipe.delete(self._sport_key(sport, "odds", eid))
        pipe.delete(ids_key)

        ordered_ids: list[str] = []
        for ev in events:
            eid = ev.get("id")
            if eid is None:
                continue
            sid = str(eid)
            ordered_ids.append(sid)
            pipe.set(
                self._sport_key(sport, "event", sid),
                json.dumps(ev, ensure_ascii=False),
            )
            if int(eid) in odds_by_id:
                pipe.set(
                    self._sport_key(sport, "odds", sid),
                    json.dumps(odds_by_id[int(eid)], ensure_ascii=False),
                )

        if ordered_ids:
            pipe.rpush(ids_key, *ordered_ids)
        await pipe.execute()

    async def list_prematch(
        self,
        sport: str,
        *,
        league_slug: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[int, Any]]:
        r = self._r()
        ids = await r.lrange(self._sport_key(sport, "event_ids"), 0, -1)
        if not ids:
            return [], {}

        pipe = r.pipeline()
        for eid in ids:
            pipe.get(self._sport_key(sport, "event", eid))
            pipe.get(self._sport_key(sport, "odds", eid))
        rows = await pipe.execute()

        events: list[dict[str, Any]] = []
        odds_map: dict[int, Any] = {}
        for i, eid in enumerate(ids):
            ev_raw = rows[i * 2]
            od_raw = rows[i * 2 + 1]
            if not ev_raw:
                continue
            ev = json.loads(ev_raw)
            if league_slug:
                lg = ev.get("league") or {}
                if (lg.get("slug") or "") != league_slug:
                    continue
            events.append(ev)
            if od_raw:
                odds_map[int(eid)] = json.loads(od_raw)
        return events, odds_map

    async def get_event(self, sport: str, event_id: int) -> dict[str, Any] | None:
        raw = await self._r().get(self._sport_key(sport, "event", str(event_id)))
        return json.loads(raw) if raw else None

    async def get_odds(self, sport: str, event_id: int) -> Any | None:
        raw = await self._r().get(self._sport_key(sport, "odds", str(event_id)))
        return json.loads(raw) if raw else None

    async def set_odds(self, sport: str, event_id: int, odds: Any) -> None:
        await self._r().set(
            self._sport_key(sport, "odds", str(event_id)),
            json.dumps(odds, ensure_ascii=False),
        )

    async def recount_odds_in_meta(self, sport: str) -> int:
        """Recount cached odds keys and update meta.odds_count."""
        r = self._r()
        ids = await r.lrange(self._sport_key(sport, "event_ids"), 0, -1)
        count = 0
        if ids:
            pipe = r.pipeline()
            for eid in ids:
                pipe.exists(self._sport_key(sport, "odds", eid))
            exists_flags = await pipe.execute()
            count = sum(1 for x in exists_flags if x)
        meta = await self.get_meta(sport) or {"sport": sport}
        meta["odds_count"] = count
        await self.set_meta(sport, meta)
        return count
