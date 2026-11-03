"""Run websocket commands of Home Assistant on behalf of the caller.

Many things can only be changed through these commands, among them everything kept
in a storage collection. Going through them means core's own schema, permission check
and logic apply. Only commands on the list below can be run.
"""

import asyncio
import contextvars
import json
from typing import Any

import probatio
from probatio.humanize import humanize_error

from homeassistant.components.websocket_api import const as websocket_const
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.http import WebSocketAdapter
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, LOGGER
from .errors import invalid_input, read_failed
from .policy.limits import CORE_COMMAND_TIMEOUT

# How lovelace answers for a dashboard that is unknown or generated, not stored.
_NOTHING_SAVED = "config_not_found"
# Each of these is kept by core as a storage collection with the same set of commands.
HELPER_KINDS = (
    "counter",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "person",
    "schedule",
    "tag",
    "timer",
    "zone",
)
REGISTRY_KINDS = ("area", "floor", "label", "category")

ALLOWED_COMMANDS = frozenset(
    {
        *(
            f"{kind}/{operation}"
            for kind in HELPER_KINDS
            for operation in ("list", "create", "update", "delete")
        ),
        *(
            f"config/{kind}_registry/{operation}"
            for kind in REGISTRY_KINDS
            for operation in ("create", "update", "delete")
        ),
        "config/device_registry/remove",
        "config/device_registry/update",
        "config/entity_registry/remove",
        "config/entity_registry/update",
        "config_entries/disable",
        "config_entries/update",
        "lovelace/config",
        "lovelace/config/delete",
        "lovelace/config/save",
        "lovelace/dashboards/create",
        "lovelace/dashboards/delete",
        "lovelace/dashboards/list",
        "lovelace/dashboards/update",
        "trace/get",
        "trace/list",
    }
)

_LOGGER = WebSocketAdapter(LOGGER, {"connid": DOMAIN})
_MESSAGE_ID = 1


def _message(command: str, fields: dict[str, Any] | None) -> dict[str, Any]:
    """Return a command as core takes it; a client cannot change which one it is."""
    return {**(fields or {}), "id": _MESSAGE_ID, "type": command}


@callback
def check_command(
    hass: HomeAssistant, command: str, fields: dict[str, Any] | None = None
) -> None:
    """Raise if a command cannot be run or core's schema does not accept it.

    Core would log such a mistake of a client as its own error. Checking first also
    gives a dry run the answer that doing it would get.
    """
    if command not in ALLOWED_COMMANDS:
        raise invalid_input("command_not_allowed", command=command)
    handler = hass.data.get(websocket_const.DOMAIN, {}).get(command)
    if handler is None:
        raise read_failed("command_unavailable", command=command)
    schema = handler[1]
    if schema is False:
        return
    message = _message(command, fields)
    try:
        schema(message)
    except probatio.Invalid as err:
        raise read_failed(
            "command_refused", command=command, message=humanize_error(message, err)
        ) from err


async def async_run_command(
    hass: HomeAssistant,
    user_id: str | None,
    command: str,
    fields: dict[str, Any] | None = None,
) -> Any:
    """Return the result of a command, which runs as the given user."""
    check_command(hass, command, fields)
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None:
        raise read_failed("command_unavailable", command=command)

    response: asyncio.Future[dict[str, Any]] = hass.loop.create_future()

    def receive(message: bytes | str | dict[str, Any]) -> None:
        """Take the one message the command answers with; it may run in a task."""
        if not response.done():
            response.set_result(
                message if isinstance(message, dict) else json.loads(message)
            )

    def start() -> None:
        connection = ActiveConnection(_LOGGER, hass, receive, user, None, None)
        connection.async_handle(_message(command, fields))

    # A connection marks the context as its own, which only holds for what it runs.
    contextvars.copy_context().run(start)
    try:
        async with asyncio.timeout(CORE_COMMAND_TIMEOUT):
            answer = await response
    except TimeoutError as err:
        raise read_failed("command_timed_out", command=command) from err
    if not answer["success"]:
        if answer["error"]["code"] == _NOTHING_SAVED:
            raise read_failed("nothing_saved", command=command)
        raise read_failed(
            "command_refused", command=command, message=answer["error"]["message"]
        )
    return answer.get("result")
