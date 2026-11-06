"""Record of the tool calls served by the integration."""

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER
from .policy.limits import MAX_CALLS_PER_MINUTE, RATE_WINDOW_SECONDS
from .policy.rate import RateLimit

_MAX_RECORDS = 100
_STORAGE_KEY = f"{DOMAIN}.audit_log"
_STORAGE_VERSION = 1
_SAVE_DELAY_SECONDS = 30


@dataclass(slots=True, frozen=True)
class AuditRecord:
    """One tool call; arguments are left out, they may hold anything.

    The exception is a reason, which a client gives for the owner to read.
    """

    timestamp: str
    tool: str
    user_id: str | None
    success: bool
    reason: str | None = None


class AuditLog:
    """Log every tool call, and keep the most recent ones across restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize an empty log."""
        self._store: Store[dict[str, list[dict[str, Any]]]] = Store(
            hass, _STORAGE_VERSION, _STORAGE_KEY
        )
        self._records: deque[AuditRecord] = deque(maxlen=_MAX_RECORDS)
        self._rate = RateLimit(
            MAX_CALLS_PER_MINUTE, RATE_WINDOW_SECONDS, "call_rate_limited"
        )

    @callback
    def require_capacity(self) -> None:
        """Raise if clients call tools too fast; a runaway loop would."""
        self._rate.require_capacity()

    async def async_load(self) -> None:
        """Load what was recorded before the last reload or restart."""
        if stored := await self._store.async_load():
            self._records.extend(AuditRecord(**record) for record in stored["records"])

    async def async_save(self) -> None:
        """Save right away, as a pending save would be lost with this object."""
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        """Remove what is stored, for when the integration is removed."""
        await self._store.async_remove()

    def _data(self) -> dict[str, list[dict[str, Any]]]:
        return {"records": [asdict(record) for record in self._records]}

    def record(
        self, tool: str, user_id: str | None, success: bool, reason: str | None = None
    ) -> None:
        """Record one finished tool call."""
        self._records.append(
            AuditRecord(dt_util.utcnow().isoformat(), tool, user_id, success, reason)
        )
        self._store.async_delay_save(self._data, _SAVE_DELAY_SECONDS)
        LOGGER.info(
            "Tool %s called by user %s: %s%s",
            tool,
            user_id,
            "success" if success else "failed",
            f", reason: {reason}" if reason else "",
        )

    def as_diagnostics(self) -> list[dict[str, str | bool]]:
        """Return the recent calls without identifying the user."""
        return [
            {
                "timestamp": record.timestamp,
                "tool": record.tool,
                "success": record.success,
                **({"reason": record.reason} if record.reason else {}),
            }
            for record in self._records
        ]
