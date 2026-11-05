"""Record of what clients changed, kept apart from the calls that only read."""

from collections import deque
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER
from .errors import invalid_input
from .policy.limits import (
    MAX_JOURNAL_RECORDS,
    MAX_WRITES_PER_MINUTE,
    WRITE_RATE_WINDOW_SECONDS,
)

_STORAGE_KEY = f"{DOMAIN}.write_journal"
_STORAGE_VERSION = 1
_SAVE_DELAY_SECONDS = 5


class WriteJournal:
    """Keep the most recent changes across restarts, and limit how fast they come."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize an empty journal."""
        self._store: Store[dict[str, list[dict[str, Any]]]] = Store(
            hass, _STORAGE_VERSION, _STORAGE_KEY
        )
        self._records: deque[dict[str, Any]] = deque(maxlen=MAX_JOURNAL_RECORDS)
        self._recent: deque[float] = deque()

    async def async_load(self) -> None:
        """Load what was recorded before the last restart."""
        if stored := await self._store.async_load():
            self._records.extend(stored["records"])

    async def async_save(self) -> None:
        """Save right away, as a pending save would be lost with this object."""
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        """Remove what is stored, for when the integration is removed."""
        await self._store.async_remove()

    def _data(self) -> dict[str, list[dict[str, Any]]]:
        return {"records": list(self._records)}

    @callback
    def require_capacity(self) -> None:
        """Raise if clients are changing too much too fast; a runaway loop would."""
        now = time.monotonic()
        while self._recent and now - self._recent[0] > WRITE_RATE_WINDOW_SECONDS:
            self._recent.popleft()
        if len(self._recent) >= MAX_WRITES_PER_MINUTE:
            raise invalid_input("write_rate_limited")
        self._recent.append(now)

    @callback
    def record(
        self,
        tool: str,
        summary: str,
        user_id: str | None,
        success: bool,
        reason: str | None,
        *,
        context_id: str | None = None,
    ) -> None:
        """Record one change that was attempted.

        The context ID is what logbook and Action history know the change by.
        """
        self._records.append(
            {
                "timestamp": dt_util.utcnow().isoformat(),
                "tool": tool,
                "summary": summary,
                "user_id": user_id,
                "success": success,
                "reason": reason,
                **({"context_id": context_id} if context_id else {}),
            }
        )
        self._store.async_delay_save(self._data, _SAVE_DELAY_SECONDS)
        LOGGER.info(
            "Change by user %s through %s: %s (%s), reason: %s",
            user_id,
            tool,
            summary,
            "done" if success else "failed",
            reason,
        )

    def recent(self) -> list[dict[str, Any]]:
        """Return the recent changes without identifying the user."""
        return [
            {key: value for key, value in record.items() if key != "user_id"}
            for record in self._records
        ]
