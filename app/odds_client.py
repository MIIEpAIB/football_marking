from typing import Any

import httpx

from app.config import ODDS_API_BASE, get_api_key


class OddsApiClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(base_url=ODDS_API_BASE, timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        api_key = get_api_key()
        q = dict(params or {})
        q["apiKey"] = api_key
        r = await self._client.get(path, params=q)
        r.raise_for_status()
        return r.json()

    async def sports(self) -> Any:
        r = await self._client.get("/sports")
        r.raise_for_status()
        return r.json()

    async def bookmakers(self) -> Any:
        r = await self._client.get("/bookmakers")
        r.raise_for_status()
        return r.json()

    async def bookmakers_selected(self) -> Any:
        return await self._get("/bookmakers/selected")

    async def leagues(self, sport: str) -> Any:
        return await self._get("/leagues", {"sport": sport})

    async def events(
        self,
        *,
        sport: str,
        league: str | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> Any:
        p: dict[str, Any] = {"sport": sport}
        if league:
            p["league"] = league
        if limit is not None:
            p["limit"] = limit
        if status:
            p["status"] = status
        return await self._get("/events", p)

    async def event_by_id(self, event_id: int) -> Any:
        return await self._get(f"/events/{event_id}")

    async def odds(
        self,
        *,
        event_id: int,
        bookmakers: str | None = None,
    ) -> Any:
        p: dict[str, Any] = {"eventId": event_id}
        if bookmakers:
            p["bookmakers"] = bookmakers
        return await self._get("/odds", p)

    async def odds_multi(
        self,
        *,
        event_ids: str,
        bookmakers: str | None = None,
    ) -> Any:
        p: dict[str, Any] = {"eventIds": event_ids}
        if bookmakers:
            p["bookmakers"] = bookmakers
        return await self._get("/odds/multi", p)
