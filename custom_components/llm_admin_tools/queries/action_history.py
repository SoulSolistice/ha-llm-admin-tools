"""Read which actions were called, from what Home Assistant keeps as Action history."""

from datetime import datetime
from typing import Any

from homeassistant.components.websocket_api import const as websocket_const
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..core_commands import async_run_command
from .compact import without_empty_fields

ACTION_CALLS_COMMAND = "logbook/get_action_calls"
_CONTEXT = "context_"
# Whether a user called it is told apart from what else did, and the user is not named.
_NOT_A_CAUSE = frozenset({"context_id", "context_user_id"})


@callback
def action_history_kept(hass: HomeAssistant) -> bool:
    """Return if Home Assistant can say which actions were called; from 2026.11."""
    return ACTION_CALLS_COMMAND in hass.data.get(websocket_const.DOMAIN, {})


def _name(entry: dict[str, Any]) -> str:
    return f"{entry['domain']}.{entry['service']}"


def _call(entry: dict[str, Any], own_contexts: frozenset[str]) -> dict[str, Any]:
    """Return one call, with what caused it, under names a client can act on."""
    caused_by = {
        key.removeprefix(_CONTEXT): value
        for key, value in entry.items()
        if key.startswith(_CONTEXT) and key not in _NOT_A_CAUSE
    }
    context_id = entry.get("context_id")
    return without_empty_fields(
        {
            "when": dt_util.utc_from_timestamp(entry["when"]),
            "action": _name(entry),
            "data": entry.get("service_data") or None,
            "by_a_user": True if entry.get("context_user_id") else None,
            "through_these_tools": True if context_id in own_contexts else None,
            "caused_by": caused_by or None,
            "context_id": context_id,
        }
    )


async def async_action_calls(
    hass: HomeAssistant,
    user_id: str | None,
    start: datetime,
    end: datetime,
    *,
    action: str | None,
    own_contexts: frozenset[str],
) -> list[dict[str, Any]]:
    """Return the actions called in a window, newest first.

    Core also returns what changed in the same contexts, which get_entity_history
    already covers.
    """
    entries = await async_run_command(
        hass,
        user_id,
        ACTION_CALLS_COMMAND,
        {"start_time": start.isoformat(), "end_time": end.isoformat()},
    )
    calls = [
        _call(entry, own_contexts)
        for entry in entries
        if "service" in entry
        and (action is None or action in (entry["domain"], _name(entry)))
    ]
    return sorted(calls, key=lambda call: call["when"], reverse=True)
