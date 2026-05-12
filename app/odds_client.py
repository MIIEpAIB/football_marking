from typing import Any

import httpx

from app.config import ODDS_API_BASE, get_api_key


def _names_from_selected_payload(data: Any) -> list[str]:
    """Normalize /bookmakers/selected response into display names for ?bookmakers=."""
    out: list[str] = []
    if isinstance(data, list):
        for x in data:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                n = x.get("name") or x.get("slug") or x.get("bookmaker") or x.get("title")
                if n:
                    out.append(str(n).strip())
    elif isinstance(data, dict):
        for key in ("bookmakers", "selected", "data", "items"):
            if key in data:
                out.extend(_names_from_selected_payload(data[key]))
                if out:
                    break
        if not out and isinstance(data.get("name"), str):
            out.append(data["name"].strip())
    return [s for s in out if s]


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

    async def selected_bookmakers_csv(self) -> str | None:
        """Comma-separated bookmakers from account selection (for /odds when caller omits bookmakers)."""
        try:
            data = await self.bookmakers_selected()
        except httpx.HTTPError:
            return None
        names = _names_from_selected_payload(data)
        return ",".join(names) if names else None

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
        bookmakers: str,
    ) -> Any:
        """bookmakers is required by upstream; pass CSV e.g. Bet365,Unibet."""
        return await self._get(
            "/odds",
            {"eventId": event_id, "bookmakers": bookmakers},
        )

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
