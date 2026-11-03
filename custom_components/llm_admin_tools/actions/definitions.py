"""Change the automations, scripts and scenes that the UI edits.

Core offers this through HTTP views only. What they do is repeated here: validate,
change the file the UI uses, then reload what is in it.
"""

import asyncio
from dataclasses import dataclass
import os
from typing import Any
import uuid

from homeassistant.components.automation.config import (
    async_validate_config_item as async_validate_automation,
)
from homeassistant.components.scene import PLATFORM_SCHEMA as SCENE_SCHEMA
from homeassistant.components.script.config import (
    async_validate_config_item as async_validate_script,
)
from homeassistant.config import (
    AUTOMATION_CONFIG_PATH,
    SCENE_CONFIG_PATH,
    SCRIPT_CONFIG_PATH,
)
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, Context, HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from ..errors import read_failed

AUTOMATION_DOMAIN = "automation"
SCENE_DOMAIN = "scene"
SCRIPT_DOMAIN = "script"

_AUTOMATION_KEY_ORDER = (
    "alias",
    "description",
    "triggers",
    "trigger",
    "conditions",
    "condition",
    "actions",
    "action",
)
_SCENE_KEY_ORDER = ("name", "entities")


@dataclass(slots=True, frozen=True)
class _Kind:
    """Where the definitions of a kind are kept."""

    path: str
    # The platform of its entities in the entity registry.
    platform: str
    # A list of items with an ID, or else a mapping with the ID as its key.
    is_list: bool
    key_order: tuple[str, ...] = ()


_KINDS = {
    AUTOMATION_DOMAIN: _Kind(
        AUTOMATION_CONFIG_PATH, AUTOMATION_DOMAIN, True, _AUTOMATION_KEY_ORDER
    ),
    SCENE_DOMAIN: _Kind(
        SCENE_CONFIG_PATH, HOMEASSISTANT_DOMAIN, True, _SCENE_KEY_ORDER
    ),
    SCRIPT_DOMAIN: _Kind(SCRIPT_CONFIG_PATH, SCRIPT_DOMAIN, False),
}
EDITABLE_KINDS = tuple(_KINDS)


def _read_file(path: str) -> Any:
    """Return the content of a file, which is empty until something is written."""
    return load_yaml(path) if os.path.isfile(path) else None


def _write_file(path: str, content: Any) -> None:
    """Replace a file."""
    write_utf8_file_atomic(path, dump(content))


def _with_id_first(spec: _Kind, key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return an item as the UI writes it, so that a person can still read the file."""
    ordered: dict[str, Any] = {CONF_ID: key}
    ordered.update({name: config[name] for name in spec.key_order if name in config})
    ordered.update(config)
    ordered[CONF_ID] = key
    return ordered


class DefinitionEditor:
    """Read and change definition files, one change at a time."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the editor."""
        self._hass = hass
        self._lock = asyncio.Lock()

    def entity_id(self, kind: str, key: str) -> str | None:
        """Return the entity of a definition, once it was loaded."""
        return er.async_get(self._hass).async_get_entity_id(
            kind, _KINDS[kind].platform, key
        )

    @staticmethod
    def resolve_key(kind: str, key: str | None) -> str:
        """Return a usable ID; a script's becomes its entity ID, so it must be given."""
        if kind == SCRIPT_DOMAIN:
            return cv.slug(key)
        return key or uuid.uuid4().hex

    def require_available(self, kind: str) -> None:
        """Raise if what would have to load a change is not running."""
        if not self._hass.services.has_service(kind, SERVICE_RELOAD):
            raise read_failed("definition_kind_unavailable", kind=kind)

    async def async_validate(self, kind: str, key: str, config: dict[str, Any]) -> None:
        """Raise if core would not accept a definition."""
        if kind == AUTOMATION_DOMAIN:
            await async_validate_automation(self._hass, key, config)
        elif kind == SCRIPT_DOMAIN:
            await async_validate_script(self._hass, key, config)
        else:
            SCENE_SCHEMA(config)

    async def _async_read(self, kind: str) -> Any:
        """Return what is in the file of a kind."""
        path = self._hass.config.path(_KINDS[kind].path)
        current = await self._hass.async_add_executor_job(_read_file, path)
        return current or ([] if _KINDS[kind].is_list else {})

    async def _async_write(self, kind: str, content: Any) -> None:
        """Replace the file of a kind."""
        path = self._hass.config.path(_KINDS[kind].path)
        await self._hass.async_add_executor_job(_write_file, path, content)

    async def async_get(self, kind: str, key: str) -> dict[str, Any] | None:
        """Return a definition as it is in its file."""
        current = await self._async_read(kind)
        found: dict[str, Any] | None = (
            next((item for item in current if item.get(CONF_ID) == key), None)
            if _KINDS[kind].is_list
            else current.get(key)
        )
        return found

    async def _async_reload(self, kind: str, key: str, context: Context | None) -> None:
        """Load what changed; for automations, that is only the changed one."""
        await self._hass.services.async_call(
            kind,
            SERVICE_RELOAD,
            {CONF_ID: key} if kind == AUTOMATION_DOMAIN else {},
            blocking=True,
            context=context,
        )

    async def async_put(
        self, kind: str, key: str, config: dict[str, Any], context: Context | None
    ) -> str | None:
        """Add or replace a definition, load it, and return its entity."""
        spec = _KINDS[kind]
        async with self._lock:
            current = await self._async_read(kind)
            if spec.is_list:
                item = _with_id_first(spec, key, config)
                indexes = [
                    index
                    for index, existing in enumerate(current)
                    if existing.get(CONF_ID) == key
                ]
                if indexes:
                    current[indexes[0]] = item
                else:
                    current.append(item)
            else:
                current[key] = config
            await self._async_write(kind, current)
        await self._async_reload(kind, key, context)
        return self.entity_id(kind, key)

    async def async_delete(self, kind: str, key: str, context: Context | None) -> None:
        """Remove a definition and its entity, and load what remains."""
        spec = _KINDS[kind]
        async with self._lock:
            current = await self._async_read(kind)
            if spec.is_list:
                current = [item for item in current if item.get(CONF_ID) != key]
            else:
                current.pop(key, None)
            await self._async_write(kind, current)
        await self._async_reload(kind, key, context)
        if (entity_id := self.entity_id(kind, key)) is not None:
            er.async_get(self._hass).async_remove(entity_id)
