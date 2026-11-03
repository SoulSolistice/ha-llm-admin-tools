"""Find what names an entity or device, in what a client could read itself."""

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import DEFAULT_DASHBOARD
from ..core_commands import async_run_command
from ..policy.exposure import ExposurePolicy
from ..policy.limits import MAX_REPORT_USES
from ..policy.members import Members, members_of
from ..serialization import to_json_safe
from .definitions import visible_definitions
from .mentions import mentions, texts_in

HOW_FOUND = (
    "Found by ID in what could be read. This does not see an ID that a template puts "
    "together, what reaches them through an area or label, or helpers, groups and "
    "template entities that read them. Only what you may see is listed. Text that "
    "is not an entity or device you may see is not looked for in withheld values."
)
_NOT_CHECKED_DEFINITIONS = "automations, scripts and scenes"
_NOT_CHECKED_DASHBOARDS = "dashboards"
Redact = Callable[[Any], Any]

# As an entity ID stands in a configuration, and not in a path or a file name.
_ENTITY_ID = re.compile(r"(?<![\w/.-])(?:states\.)?([a-z_]+\.[a-z0-9_]+)(?![\w/-])")
# These hold the name of a trigger, condition or service, which reads like an entity ID
# in the same domain, as light.turned_on does.
_NAMES_NO_ENTITY = frozenset(
    {"action", "condition", "perform_action", "service", "trigger"}
)


@dataclass(slots=True, frozen=True)
class Sources:
    """What could be read to look in, and what could not."""

    items: list[tuple[str, str, Any]]
    not_checked: list[str]


async def _async_dashboards(
    hass: HomeAssistant, user_id: str | None
) -> tuple[list[tuple[str, str, Any]], bool]:
    """Return what is on the dashboards, and if all of them could be read."""
    try:
        listed = await async_run_command(hass, user_id, "lovelace/dashboards/list")
    except HomeAssistantError:
        return [], False
    sources = []
    complete = True
    for url_path in (None, *(dashboard["url_path"] for dashboard in listed)):
        try:
            config = await async_run_command(
                hass, user_id, "lovelace/config", {"url_path": url_path}
            )
        except HomeAssistantError as err:
            # A dashboard that was never saved is generated, so it names nothing.
            complete = complete and err.translation_key == "nothing_saved"
        else:
            sources.append(("dashboard", url_path or DEFAULT_DASHBOARD, config))
    return sources, complete


async def async_read_sources(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    user_id: str | None,
    *,
    read_definitions: bool,
    read_dashboards: bool,
) -> Sources:
    """Return what to look in, reading only what a client may read."""
    items = visible_definitions(hass, policy) if read_definitions else []
    not_checked = [] if read_definitions else [_NOT_CHECKED_DEFINITIONS]
    complete = False
    if read_dashboards:
        dashboards, complete = await _async_dashboards(hass, user_id)
        items = [*items, *dashboards]
    if not complete:
        not_checked.append(_NOT_CHECKED_DASHBOARDS)
    return Sources(items, not_checked)


def _row(
    kind: str,
    source_id: str,
    label: str,
    names: list[str],
    max_names: int | None = MAX_REPORT_USES,
) -> dict[str, Any]:
    """Return one source with what it names, and how many of those there are."""
    return {
        "kind": kind,
        "id": source_id,
        label: names[:max_names],
        f"{label}_count": len(names),
    }


def _by_kind(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the rows in a fixed order."""
    return sorted(rows, key=lambda row: (row["kind"], row["id"]))


def find_uses(sources: Sources, names: dict[str, str]) -> list[dict[str, Any]]:
    """Return which sources name one of these; this can be a lot of text."""
    used_by = []
    for kind, source_id, config in sources.items:
        mentioned = mentions(config)
        uses = sorted({name for key, name in names.items() if key in mentioned})
        if uses:
            used_by.append(_row(kind, source_id, "uses", uses))
    return _by_kind(used_by)


def names_of_members(members: Members) -> dict[str, str]:
    """Return what these can be named by, and what to call them then.

    A device trigger names a device, or an entity by the ID of its registry entry.
    """
    return {
        **{entry.entity_id.lower(): entry.entity_id for entry in members.entities},
        **{entry.id.lower(): entry.entity_id for entry in members.entities},
        **{entity_id.lower(): entity_id for entity_id in members.unregistered_entities},
        **{device.id.lower(): device.id for device in members.devices},
    }


@callback
def _known_names(
    hass: HomeAssistant, policy: ExposurePolicy, target: str
) -> dict[str, str] | None:
    """Return what an entity or device a client may see is named by, if it is one."""
    entry = er.async_get(hass).async_get(target)
    if entry is not None and policy.entity_allowed(entry.entity_id):
        return {target.lower(): target, **names_of_members(Members([entry], []))}
    device = dr.async_get(hass).async_get(target)
    if device is not None and policy.device_allowed(device):
        members = members_of(hass, [], [device])
        return {
            target.lower(): target,
            **names_of_members(
                Members(
                    [e for e in members.entities if policy.entity_allowed(e.entity_id)],
                    [d for d in members.devices if policy.device_allowed(d)],
                )
            ),
        }
    if hass.states.get(target) is not None and policy.entity_allowed(target):
        return {target.lower(): target}
    return None


def _readable(sources: Sources, redact: Redact) -> Sources:
    """Return the sources as a client could read them; this can be a lot of text."""
    return Sources(
        [
            (kind, source_id, redact(to_json_safe(config)))
            for kind, source_id, config in sources.items
        ],
        sources.not_checked,
    )


async def async_find_target(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    sources: Sources,
    target: str,
    redact: Redact,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Return which sources name a target, and the names that were looked for.

    Something a client may see is also found where it stands under a withheld key, as in
    code: "{{ states('input_text.alarm') }}", and looking for that gives nothing away.
    Anything else is only what a client typed, which could be a guess at a withheld
    value, so it is only looked for in what a client could read. That includes what is
    hidden, which so gets the same answer as what does not exist.
    """
    names = _known_names(hass, policy, target)
    if names is not None:
        rows = await hass.async_add_executor_job(find_uses, sources, names)
        return rows, set(names)
    names = {target.lower(): target}
    readable = await hass.async_add_executor_job(_readable, sources, redact)
    rows = await hass.async_add_executor_job(find_uses, readable, names)
    return rows, set(names)


@dataclass(slots=True, frozen=True)
class _Known:
    """What exists, read in the event loop and looked in outside it."""

    entities: set[str]
    domains: set[str]
    services: dict[str, set[str]]


@callback
def _known(hass: HomeAssistant) -> _Known:
    """Return what exists right now, also what is disabled or not loaded."""
    entities = {*hass.states.async_entity_ids(), *er.async_get(hass).entities}
    return _Known(
        entities,
        {entity_id.partition(".")[0] for entity_id in entities},
        {
            domain: set(services)
            for domain, services in hass.services.async_services().items()
        },
    )


def _is_missing(name: str, known: _Known) -> bool:
    """Return if a name looks like an entity that does not exist.

    A domain without any entities says that this is something else, such as a variable.
    What Home Assistant would refuse as an entity ID is something else too: a chart adds
    two underscores to an ID of its own, and light.turn_{{ state }} stops at one.
    """
    domain, _, rest = name.partition(".")
    return (
        valid_entity_id(name)
        and domain in known.domains
        and name not in known.entities
        and rest not in known.services.get(domain, ())
    )


def _find_missing(
    sources: Sources, known: _Known, max_names: int | None
) -> list[dict[str, Any]]:
    """Return which sources name an entity that does not exist."""
    rows = []
    for kind, source_id, config in sources.items:
        missing = sorted(
            {
                match[1]
                for text in texts_in(
                    to_json_safe(config), except_text_under=_NAMES_NO_ENTITY
                )
                for match in _ENTITY_ID.finditer(text.lower())
                if _is_missing(match[1], known)
            }
        )
        if missing:
            rows.append(_row(kind, source_id, "missing", missing, max_names))
    return _by_kind(rows)


async def async_find_missing(
    hass: HomeAssistant,
    sources: Sources,
    redact: Redact,
    *,
    max_names: int | None = None,
) -> list[dict[str, Any]]:
    """Return which sources name an entity that does not exist.

    This returns what it finds as it stands, so a withheld value must not be among it.
    """
    known = _known(hass)

    def find() -> list[dict[str, Any]]:
        return _find_missing(_readable(sources, redact), known, max_names)

    return await hass.async_add_executor_job(find)
