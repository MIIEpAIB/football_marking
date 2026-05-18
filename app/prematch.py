from datetime import datetime, timezone
from typing import Any


def is_prematch_event(ev: dict[str, Any]) -> bool:
    st = (ev.get("status") or "").lower()
    if st in ("live", "settled"):
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
