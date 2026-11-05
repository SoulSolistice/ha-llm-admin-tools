"""Keep what a client writes within what the settings let it change.

An automation runs later, with all rights, on behalf of nobody. If it could do what
its author cannot, every other limit would mean nothing.
"""

from collections.abc import Mapping
from enum import Enum, auto
import re
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import is_template_string

from ..errors import invalid_input
from .writes import WritePolicy

_REFUSED_KEYS = frozenset(
    {"action_template", "event_template", "service_template", "use_blueprint"}
)
_SERVICE_KEYS = ("action", "service")
_TARGET_KEYS = frozenset(
    {"area_id", "device_id", "entity_id", "exclude", "floor_id", "label_id", "target"}
)
# From 2026.11 a target can leave out what it would otherwise reach.
_EXCLUDE_KEY = "exclude"
_GROUP_TARGET_KEYS = ("area_id", "floor_id", "label_id")
# In entities, a mapping is a scene: a state for each entity.
_ENTITY_KEYS = ("entity_id", "entities", "snapshot_entities")
_READING_KEYS = frozenset(
    {
        "condition",
        "conditions",
        "if",
        "trigger",
        "triggers",
        "until",
        "wait_for_trigger",
        "while",
    }
)
# Core renders these whole, so a template in them can come to name any target.
_RENDERED_WHOLE_KEYS = ("data", "data_template", "target")
_DASHBOARD_SERVICE_KEYS = ("perform_action", "service")
# A browser skips these in an address, and reads a backslash as a slash.
_DASHBOARD_IGNORED_TEXT = re.compile(r"[\x00-\x1f\x7f]")
# What makes a browser reach outside when someone opens a dashboard, what spells that in
# a way that would not be seen here, and what custom cards run in the browser.
_DASHBOARD_REFUSED_TEXT = re.compile(
    r"\b(?:https?|ftp|wss?|javascript|vbscript|blob):"
    r"|\bdata:\s*[\w.+-]+/|\bdata:[^,\s]*,"
    r"|(?<![:\w])//[^\s/]"
    r"|&#|&\w+;"
    r"|\[\[\[",
    re.I,
)
_DATA_KEYS = frozenset(
    {
        "data",
        "data_template",
        "event_data",
        "event_data_template",
        "fields",
        "metadata",
        "target",
        "trigger_variables",
        "variables",
    }
)


class _Part(Enum):
    """What the part of a definition being looked at does."""

    ACTION = auto()
    # Values that an action passes on: an action key in here means nothing.
    DATA = auto()
    # Triggers and conditions only look at things.
    READING = auto()


def _names_in(value: Any) -> list[str]:
    """Return the names in a field: one, several, or the keys of a mapping.

    Anything else that core would accept has no name in it to check.
    """
    if isinstance(value, Mapping):
        value = list(value)
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, str)]


class _Guard:
    """Walk through configuration, raising at what is not allowed."""

    def __init__(self, policy: WritePolicy, *, templates_run: bool) -> None:
        """Initialize the walk; only definitions have their templates rendered."""
        self._policy = policy
        self._templates_run = templates_run

    def walk(self, node: Any, part: _Part) -> None:
        """Check a value and everything in it."""
        if isinstance(node, list):
            for item in node:
                self.walk(item, part)
        elif isinstance(node, Mapping):
            self._walk_mapping(node, part)
        elif self._templates_run and isinstance(node, str) and is_template_string(node):
            self._policy.require_templates()

    def _walk_mapping(self, node: Mapping[str, Any], part: _Part) -> None:
        """Check a mapping, then what is in it according to what each part does."""
        if part is _Part.ACTION and isinstance(node.get("condition"), str):
            part = _Part.READING
        if not _REFUSED_KEYS.isdisjoint(node):
            raise invalid_input("definition_part_refused")
        if part is _Part.ACTION:
            self._check_action(node)
        if self._templates_run and part is not _Part.READING:
            self._check_rendered_names(node, part)
        self._check_targets(node, part)
        for key, value in node.items():
            self.walk(key, part)
            self.walk(value, self._part_of(key, part))

    @staticmethod
    def _part_of(key: str, part: _Part) -> _Part:
        """Return what the value of a key does, within the part it is in."""
        if key == _EXCLUDE_KEY:
            # What it names is left alone, so it only needs to be one that may be seen.
            return _Part.READING
        if part is not _Part.ACTION:
            return part
        if key in _READING_KEYS:
            return _Part.READING
        return _Part.DATA if key in _DATA_KEYS else part

    def _check_rendered_names(self, node: Mapping[str, Any], part: _Part) -> None:
        """Check what core renders into names of targets, which is only known then.

        Core also renders the keys of data, so one can become entity_id.
        """
        names = [key for key in node if isinstance(key, str)]
        if part is _Part.ACTION:
            names += [
                value
                for key in _RENDERED_WHOLE_KEYS
                if isinstance(value := node.get(key), str)
            ]
        for name in names:
            self._refuse_template_if_restricted(name)

    def _check_action(self, node: Mapping[str, Any]) -> None:
        """Check what an action would run."""
        for key in _SERVICE_KEYS:
            if isinstance(service := node.get(key), str):
                self.check_service(service)
        if isinstance(node.get("event"), str):
            self._policy.require_service("event", "fire")
        if isinstance(scene := node.get("scene"), str):
            self._policy.require_service("scene", "turn_on")
            self._check_entity_field(scene, _Part.ACTION)
        if "device_id" in node and "domain" in node and "type" in node:
            self._policy.require_service(
                str(node["domain"]).lower(), str(node["type"]).lower()
            )

    def check_service(self, service: str) -> None:
        """Check a service an action names; a template could become any service."""
        if is_template_string(service):
            raise invalid_input("definition_part_refused")
        domain, _, name = service.strip().lower().partition(".")
        self._policy.require_service(domain, name)
        self._policy.require_service_target(domain, name)

    def _check_targets(self, node: Mapping[str, Any], part: _Part) -> None:
        """Check what is named as the target of something, directly or as a group."""
        for key in _ENTITY_KEYS:
            for value in _names_in(node.get(key)):
                self._check_entity_field(value, part)
        for value in _names_in(node.get("device_id")):
            self._check_device(value, part)
        if (
            part is not _Part.READING
            and self._policy.restricted
            and any(key in node for key in _GROUP_TARGET_KEYS)
        ):
            # Who is in a group is only known when it runs.
            raise invalid_input("group_target_refused")

    def _refuse_template_if_restricted(self, value: str) -> bool:
        """Return if a field is a template, which allows it only if nothing is limited.

        What it renders to is only known when it runs.
        """
        if not is_template_string(value):
            return False
        if self._policy.restricted:
            raise invalid_input("group_target_refused")
        return True

    def _check_entity_field(self, value: str, part: _Part) -> None:
        """Check a field naming one entity or several, separated by commas."""
        if not self._refuse_template_if_restricted(value):
            for entity_id in value.split(","):
                self._check_entity(entity_id, part)

    def _check_entity(self, value: str, part: _Part) -> None:
        """Check one entity, named by entity ID or the ID of its registry entry."""
        name = value.strip().lower()
        if name == "none":
            return
        if name == "all":
            raise invalid_input("all_entities_refused")
        entry = er.async_get(self._policy.hass).async_get(name)
        entity_id = entry.entity_id if entry is not None else name
        if part is _Part.READING:
            self._policy.require_visible_entity(entity_id)
        else:
            self._policy.require_target(entity_id)

    def _check_device(self, device_id: str, part: _Part) -> None:
        """Check one device."""
        if self._refuse_template_if_restricted(device_id):
            return
        if part is _Part.READING:
            self._policy.require_visible_device(device_id)
        else:
            self._policy.require_device(device_id)


class _DashboardGuard:
    """Walk through a dashboard, raising at what is not allowed.

    It runs in the browser of whoever opens it, so unlike what a client can do itself,
    it can read what is hidden, and tell the outside. Buttons call services for them.
    """

    def __init__(self, policy: WritePolicy) -> None:
        """Initialize the walk."""
        self._policy = policy
        self._actions = _Guard(policy, templates_run=True)

    def walk(self, node: Any) -> None:
        """Check a value and everything in it."""
        if isinstance(node, list):
            for item in node:
                self.walk(item)
        elif isinstance(node, Mapping):
            self._check_action(node)
            for key, value in node.items():
                self.walk(key)
                self.walk(value)
        elif isinstance(node, str):
            self._check_text(node)

    def _check_text(self, text: str) -> None:
        """Check a text, which may be shown as a link or an image, or be rendered."""
        as_read = _DASHBOARD_IGNORED_TEXT.sub("", text).replace("\\", "/")
        if _DASHBOARD_REFUSED_TEXT.search(as_read):
            raise invalid_input("dashboard_link_refused")
        if is_template_string(text):
            self._policy.require_templates()

    def _check_action(self, node: Mapping[str, Any]) -> None:
        """Check what pressing something would call."""
        services = [
            service
            for key in _DASHBOARD_SERVICE_KEYS
            if isinstance(service := node.get(key), str)
        ]
        for service in services:
            self._actions.check_service(service)
        if services:
            self._actions.walk(node, _Part.DATA)


@callback
def check_dashboard(policy: WritePolicy, config: Mapping[str, Any]) -> None:
    """Raise unless a dashboard stays within what is allowed."""
    _DashboardGuard(policy).walk(config)


@callback
def check_definition(policy: WritePolicy, config: Mapping[str, Any]) -> None:
    """Raise unless an automation, script or scene stays within what is allowed."""
    _Guard(policy, templates_run=True).walk(config, _Part.ACTION)


@callback
def check_service_data(policy: WritePolicy, data: Mapping[str, Any]) -> None:
    """Raise unless the data of a service call stays within what is allowed.

    The targets are stated separately, so that they can all be checked.
    """
    if not _TARGET_KEYS.isdisjoint(data):
        raise invalid_input("target_in_service_data")
    _Guard(policy, templates_run=False).walk(data, _Part.DATA)
