"""Call a service."""

import asyncio
from functools import partial
from typing import Any

from homeassistant.core import Context, HomeAssistant, SupportsResponse

from ..const import LOGGER
from ..policy.limits import SERVICE_CALL_TIMEOUT


def _note_late_failure(name: str, call: asyncio.Task[Any]) -> None:
    """Log that a call failed after the client was told that it had not ended."""
    if not call.cancelled() and (err := call.exception()) is not None:
        LOGGER.warning("%s failed after it took too long: %s", name, type(err).__name__)


async def async_call_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    data: dict[str, Any],
    context: Context | None,
) -> dict[str, Any]:
    """Call a service and return what it answered, if it finishes in time.

    If it takes longer it keeps running, because stopping something halfway is worse
    than not knowing how it ended.
    """
    responds = hass.services.supports_response(domain, service)
    call = hass.async_create_task(
        hass.services.async_call(
            domain,
            service,
            data,
            blocking=True,
            context=context,
            return_response=responds is not SupportsResponse.NONE,
        )
    )
    await asyncio.wait({call}, timeout=SERVICE_CALL_TIMEOUT)
    if not call.done():
        call.add_done_callback(partial(_note_late_failure, f"{domain}.{service}"))
        return {"finished": False}
    response = call.result()
    return {"finished": True, **({"response": response} if response else {})}
