"""Tests for what each tool reads."""

from datetime import timedelta
from enum import Enum
import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.policy.limits import MAX_ATTRIBUTE_CHARACTERS
from custom_components.llm_admin_tools.serialization import to_json_safe
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import HA_2026_11, CallTool


async def test_get_system_info(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Facts about the instance come back, private details do not."""
    result = await call_tool("get_system_info")
    assert result.data["version"]
    assert result.data["recovery_mode"] is False
    assert "user" not in result.data
    assert "config_dir" not in result.data


@pytest.fixture
def populated(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> dict[str, str]:
    """Create an area, a device and entities."""
    area = area_registry.async_create("Cellar")
    device = device_registry.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={("test", "boiler")},
        manufacturer="Acme",
        model="B1",
        name="Boiler",
        serial_number="SERIAL-123",
    )
    device_registry.async_update_device(device.id, area_id=area.id)
    entry = entity_registry.async_get_or_create(
        "sensor",
        "test",
        "temperature",
        device_id=device.id,
        original_name="Temperature",
    )
    hass.states.async_set(
        entry.entity_id,
        "21",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    hass.states.async_set("light.other", "on", {"friendly_name": "Hall lamp"})
    return {"area": area.id, "device": device.id, "entity": entry.entity_id}


async def test_list_entities_all(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """Entities come back sorted, with the area inherited from the device."""
    result = await call_tool("list_entities")
    assert [item["entity_id"] for item in result.data["items"]] == [
        "light.other",
        populated["entity"],
    ]
    assert result.data["total_count"] == 2
    assert result.data["next_offset"] is None
    sensor = result.data["items"][1]
    assert sensor["area_id"] == populated["area"]
    assert sensor["device_id"] == populated["device"]
    assert sensor["unit"] == "°C"


async def test_list_entities_leaves_out_empty_fields(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """A field that is not set is absent, and the time only comes when asked for."""
    plain = (await call_tool("list_entities", domain="light")).data["items"][0]
    assert plain == {"entity_id": "light.other", "name": "Hall lamp", "state": "on"}

    timed = await call_tool("list_entities", domain="light", include_last_changed=True)
    assert isinstance(timed.data["items"][0]["last_changed"], str)


async def test_list_entities_leaves_out_background_entities(
    hass: HomeAssistant,
    populated: dict[str, str],
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """Config, diagnostic and hidden entities come when asked for, and are counted."""
    background = [
        entity_registry.async_get_or_create(
            "sensor", "test", "diag", entity_category=er.EntityCategory.DIAGNOSTIC
        ),
        entity_registry.async_get_or_create(
            "number", "test", "conf", entity_category=er.EntityCategory.CONFIG
        ),
        entity_registry.async_get_or_create(
            "sensor", "test", "hid", hidden_by=er.RegistryEntryHider.USER
        ),
    ]
    for entry in background:
        hass.states.async_set(entry.entity_id, "1")

    usual = await call_tool("list_entities")
    assert [item["entity_id"] for item in usual.data["items"]] == [
        "light.other",
        populated["entity"],
    ]
    assert usual.data["total_count"] == 2
    assert usual.data["left_out_by_default"] == 3

    # Only what matches the other filters counts.
    numbers = await call_tool("list_entities", domain="number")
    assert numbers.data["items"] == []
    assert numbers.data["left_out_by_default"] == 1

    everything = await call_tool("list_entities", include_diagnostic=True)
    assert everything.data["total_count"] == 5
    assert everything.data["left_out_by_default"] == 0

    # Asking for one by name still works.
    direct = await call_tool("get_entity", entity_id=background[0].entity_id)
    assert direct.data["state"] == "1"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"domain": "light"}, ["light.other"]),
        ({"state": "on"}, ["light.other"]),
        ({"search": "hall LAMP"}, ["light.other"]),
        ({"search": "OTHER"}, ["light.other"]),
        ({"search": "nothing"}, []),
    ],
)
async def test_list_entities_filters(
    populated: dict[str, str],
    call_tool: CallTool,
    args: dict[str, Any],
    expected: list[str],
) -> None:
    """The filters narrow the result."""
    result = await call_tool("list_entities", **args)
    assert [item["entity_id"] for item in result.data["items"]] == expected


async def test_list_entities_by_area_and_device(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """Area and device filters match the sensor only."""
    for args in ({"area_id": populated["area"]}, {"device_id": populated["device"]}):
        result = await call_tool("list_entities", **args)
        assert [item["entity_id"] for item in result.data["items"]] == [
            populated["entity"]
        ]


async def test_list_entities_paging(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """Pages say where the next one starts."""
    first = await call_tool("list_entities", limit=1)
    assert first.data["next_offset"] == 1
    assert first.data["total_count"] == 2
    second = await call_tool("list_entities", limit=1, offset=1)
    assert second.data["next_offset"] is None
    assert second.data["items"][0]["entity_id"] == populated["entity"]


async def test_area_of_entity_itself_wins(
    hass: HomeAssistant,
    populated: dict[str, str],
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """An area set on the entity overrides the one of its device."""
    garage = area_registry.async_create("Garage")
    entity_registry.async_update_entity(populated["entity"], area_id=garage.id)
    result = await call_tool("get_entity", entity_id=populated["entity"])
    assert result.data["registry"]["area_id"] == garage.id


async def test_entity_of_removed_device_has_no_area(
    hass: HomeAssistant,
    populated: dict[str, str],
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """A device ID that no longer resolves gives no area and no error."""
    other = entity_registry.async_get_or_create("sensor", "test", "plain")
    hass.states.async_set(other.entity_id, "1")
    result = await call_tool("get_entity", entity_id=other.entity_id)
    assert result.data["registry"]["area_id"] is None


async def test_get_entity(populated: dict[str, str], call_tool: CallTool) -> None:
    """State, attributes and registry facts come together."""
    result = await call_tool("get_entity", entity_id=populated["entity"])
    assert result.data["state"] == "21"
    assert result.data["attributes"]["device_class"] == "temperature"
    assert result.data["registry"]["platform"] == "test"
    assert result.data["registry"]["device_id"] == populated["device"]


async def test_entities_are_found_by_their_other_names(
    hass: HomeAssistant,
    populated: dict[str, str],
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """Users and voice know an entity by its aliases, and by its name before 2026.11."""
    entity_id = populated["entity"]
    entity_registry.async_update_entity(
        entity_id, aliases=[er.COMPUTED_NAME, "Heizung", "Kessel"]
    )
    if HA_2026_11:
        entity_registry._async_update_entity(entity_id, compat_name="Boiler water")
    found = await call_tool("list_entities", search="kessel")
    assert [item["entity_id"] for item in found.data["items"]] == [entity_id]
    registry = (await call_tool("get_entity", entity_id=entity_id)).data["registry"]
    # The alias that stands for its own name is not repeated.
    assert registry["other_names"][:2] == ["Heizung", "Kessel"]
    assert len(registry["other_names"]) == (3 if HA_2026_11 else 2)
    if HA_2026_11:
        assert registry["other_names"][2] == "Boiler water"
        found = await call_tool("list_entities", search="boiler water")
        assert [item["entity_id"] for item in found.data["items"]] == [entity_id]


async def test_get_entity_shortens_long_attributes(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Long attributes are cut and reported; the others are untouched."""
    limit = MAX_ATTRIBUTE_CHARACTERS
    text = "x" * (limit * 2)
    items = [{"day": index} for index in range(limit)]
    mapping = {f"key{index}": index for index in range(limit)}
    hass.states.async_set(
        "sensor.a", "1", {"text": text, "items": items, "map": mapping, "short": "ok"}
    )

    result = await call_tool("get_entity", entity_id="sensor.a")
    attributes = result.data["attributes"]
    assert attributes["short"] == "ok"
    assert attributes["text"] == text[:limit]
    assert attributes["items"] == items[: len(attributes["items"])]
    assert 0 < len(attributes["items"]) < len(items)
    assert 0 < len(attributes["map"]) < len(mapping)
    assert len(json.dumps(attributes)) < limit * 4
    assert result.data["shortened_attributes"]["text"] == len(text) + 2
    assert set(result.data["shortened_attributes"]) == {"text", "items", "map"}

    named = await call_tool(
        "get_entity", entity_id="sensor.a", attributes=["items", "absent"]
    )
    assert named.data["attributes"] == {"items": items}
    assert "shortened_attributes" not in named.data


async def test_get_entity_without_long_attributes_reports_nothing(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """Nothing is said about shortening when nothing was cut."""
    result = await call_tool("get_entity", entity_id=populated["entity"])
    assert "shortened_attributes" not in result.data


async def test_shortening_cannot_expose_part_of_a_secret(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A secret across the cut is removed whole, because redaction runs first."""
    secret = "hunter2-the-secret"
    # Without redaction first, the cut would leave "hunter2-the-" behind.
    text = "x" * (MAX_ATTRIBUTE_CHARACTERS - 12) + secret + "y" * 3000
    hass.states.async_set("sensor.a", "1", {"text": text, "password": "plain"})

    result = await call_tool("get_entity", entity_id="sensor.a")
    assert "hunter2" not in json.dumps(result.data)

    named = await call_tool(
        "get_entity", entity_id="sensor.a", attributes=["password", "text"]
    )
    assert named.data["attributes"]["password"] == "**REDACTED**"
    assert "hunter2" not in json.dumps(named.data)


async def test_get_entity_without_registry(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """An entity known only by its state is still found."""
    result = await call_tool("get_entity", entity_id="light.other")
    assert result.data["registry"] is None


async def test_get_disabled_entity(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    call_tool: CallTool,
) -> None:
    """A disabled entity has a registry entry and no state."""
    entry = entity_registry.async_get_or_create(
        "sensor", "test", "off", disabled_by=er.RegistryEntryDisabler.USER
    )
    result = await call_tool("get_entity", entity_id=entry.entity_id)
    assert result.data["state"] is None
    assert result.data["last_changed"] is None
    assert result.data["attributes"] == {}
    assert result.data["registry"]["disabled_by"] == "user"


async def test_get_entity_not_found(call_tool: CallTool) -> None:
    """An unknown entity is a clear error."""
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_entity", entity_id="sensor.none")
    assert err.value.translation_key == "entity_not_found"


async def test_list_devices(populated: dict[str, str], call_tool: CallTool) -> None:
    """Devices come back without serial numbers."""
    result = await call_tool("list_devices")
    assert result.data["total_count"] == 1
    device = result.data["items"][0]
    assert device["name"] == "Boiler"
    assert device["manufacturer"] == "Acme"
    assert device["config_entry_id"]
    assert "SERIAL-123" not in json.dumps(result.data)


async def test_list_devices_by_area(
    populated: dict[str, str], call_tool: CallTool
) -> None:
    """The area filter applies."""
    match = await call_tool("list_devices", area_id=populated["area"])
    assert match.data["total_count"] == 1
    other = await call_tool("list_devices", area_id="elsewhere")
    assert other.data["total_count"] == 0


async def test_list_areas(populated: dict[str, str], call_tool: CallTool) -> None:
    """Areas are listed."""
    result = await call_tool("list_areas")
    assert result.data["items"][0]["name"] == "Cellar"


async def test_list_integrations(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Status comes back; what the entry stores never does."""
    MockConfigEntry(
        domain="other",
        title="Other",
        data={"unlisted_name": "stored-value"},
        options={"more": "stored-option"},
    ).add_to_hass(hass)
    everything = await call_tool("list_integrations")
    assert {item["domain"] for item in everything.data["items"]} == {
        "llm_admin_tools",
        "other",
    }
    assert "stored-" not in json.dumps(everything.data)

    only_other = await call_tool("list_integrations", domain="other")
    assert only_other.data["items"][0]["state"] == "not_loaded"
    assert only_other.data["total_count"] == 1


async def test_definitions(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Automations and scripts are listed and read, with secrets withheld."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "a1",
                "alias": "Night",
                "trigger": {"platform": "event", "event_type": "go"},
                "action": {
                    "action": "notify.send",
                    "data": {"password": "plain", "message": "hunter2-the-secret"},
                },
            }
        },
    )
    assert await async_setup_component(
        hass,
        "script",
        {"script": {"s1": {"alias": "Warm", "sequence": [{"delay": "00:00:01"}]}}},
    )
    await hass.async_block_till_done()

    listed = await call_tool("list_definitions", kind="automation")
    assert listed.data["items"][0]["entity_id"] == "automation.night"
    assert "config" not in listed.data["items"][0]

    read = await call_tool(
        "get_definition", kind="automation", entity_id="automation.night"
    )
    action = read.data["config"]["action"]
    assert action["data"]["password"] == "**REDACTED**"
    assert action["data"]["message"] == "**REDACTED**"

    script = await call_tool("get_definition", kind="script", entity_id="script.s1")
    assert script.data["config"]["alias"] == "Warm"


async def test_definitions_when_component_not_loaded(call_tool: CallTool) -> None:
    """A kind that is not loaded lists nothing and finds nothing."""
    assert (await call_tool("list_definitions", kind="script")).data["items"] == []
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_definition", kind="script", entity_id="script.none")
    assert err.value.translation_key == "entity_not_found"


async def test_definition_of_another_kind(call_tool: CallTool) -> None:
    """An entity ID that does not fit the kind says so."""
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_definition", kind="script", entity_id="automation.night")
    assert err.value.translation_key == "definition_kind_mismatch"


async def test_definition_without_config(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """An entity that holds no configuration is reported."""
    assert await async_setup_component(
        hass,
        "script",
        {"script": {"s1": {"sequence": []}}},
    )
    await hass.async_block_till_done()
    component = hass.data["entity_components"]["script"]
    component.get_entity("script.s1").raw_config = None
    with pytest.raises(HomeAssistantError) as err:
        await call_tool("get_definition", kind="script", entity_id="script.s1")
    assert err.value.translation_key == "definition_unavailable"


async def test_system_log(hass: HomeAssistant, call_tool: CallTool) -> None:
    """Log records are filtered and scrubbed."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    logger = logging.getLogger("test.component")
    logger.warning("a warning about the boiler")
    logger.error("login failed password=abc123")
    logging.getLogger("other.module").error("unrelated")
    await hass.async_block_till_done()

    everything = await call_tool("get_system_log")
    assert everything.data["total_count"] == 3
    assert everything.data["items"][0]["message"] == ["unrelated"]
    assert "abc123" not in json.dumps(everything.data)

    errors = await call_tool("get_system_log", min_level="ERROR")
    assert errors.data["total_count"] == 2

    by_logger = await call_tool("get_system_log", logger="test.component")
    assert by_logger.data["total_count"] == 2

    by_text = await call_tool("get_system_log", search="BOILER")
    assert by_text.data["total_count"] == 1

    # The same format as the other tools, not seconds since the epoch.
    assert dt_util.parse_datetime(everything.data["items"][0]["timestamp"])
    assert dt_util.parse_datetime(everything.data["items"][0]["first_occurred"])


async def test_system_log_search_covers_exceptions(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """The cause of an error is usually only in its exception."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()
    try:
        raise ValueError("the real cause")
    except ValueError:
        logging.getLogger("test.component").exception("it failed")
    await hass.async_block_till_done()

    result = await call_tool("get_system_log", search="REAL CAUSE")
    assert result.data["total_count"] == 1


async def test_system_log_unavailable(call_tool: CallTool) -> None:
    """A missing system log is a clear error."""
    with pytest.raises(HomeAssistantError) as err:
        await call_tool("get_system_log")
    assert err.value.translation_key == "system_log_unavailable"


async def test_history_without_recorder(call_tool: CallTool) -> None:
    """A missing recorder is a clear error."""
    with pytest.raises(HomeAssistantError) as err:
        await call_tool("get_entity_history", entity_ids=["sensor.a"])
    assert err.value.translation_key == "recorder_unavailable"


@pytest.mark.parametrize(
    ("args", "key"),
    [
        ({"start": "not a time"}, "invalid_time"),
        (
            {"start": "2026-01-02T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"},
            "invalid_time_window",
        ),
        (
            {"start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00"},
            "time_window_too_long",
        ),
    ],
)
async def test_history_bad_window(
    call_tool: CallTool, args: dict[str, str], key: str
) -> None:
    """Bad time windows are refused before anything is read."""
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_entity_history", entity_ids=["sensor.a"], **args)
    assert err.value.translation_key == key


async def test_history_malformed_entity_id(call_tool: CallTool) -> None:
    """A malformed entity ID is refused before the recorder sees it."""
    with pytest.raises(ServiceValidationError) as err:
        await call_tool("get_entity_history", entity_ids=["not an entity"])
    assert err.value.translation_key == "invalid_entity_id"


async def test_history_too_many_entities(call_tool: CallTool) -> None:
    """The number of entities is bounded."""
    with pytest.raises(Exception, match="entity_ids"):
        await call_tool(
            "get_entity_history", entity_ids=[f"sensor.s{i}" for i in range(11)]
        )


async def test_history_from_recorder(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """State changes are read from the real recorder."""
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    hass.states.async_set("sensor.a", "1")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    hass.states.async_set("sensor.a", "2")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    now = dt_util.utcnow()
    result = await call_tool(
        "get_entity_history",
        entity_ids=["sensor.a", "sensor.unknown"],
        start=(now - timedelta(hours=1)).isoformat(),
        end=(now + timedelta(minutes=1)).isoformat(),
    )
    entities = result.data["entities"]
    assert [point["state"] for point in entities["sensor.a"]["points"]] == ["1", "2"]
    assert entities["sensor.a"]["exists"] is True
    assert entities["sensor.a"]["older_changes_left_out"] is False
    assert entities["sensor.a"]["covered_from"]
    assert entities["sensor.unknown"] == {
        "exists": False,
        "points": [],
        "older_changes_left_out": False,
        "covered_from": None,
    }

    default_window = await call_tool("get_entity_history", entity_ids=["sensor.a"])
    assert len(default_window.data["entities"]["sensor.a"]["points"]) == 2


async def test_longest_history_window_survives_the_time_a_request_takes(
    recorder_mock: Any, hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Asking for the last 7 days must not fail over the moments that takes."""
    now = dt_util.utcnow()
    result = await call_tool(
        "get_entity_history",
        entity_ids=["sensor.a"],
        start=(now - timedelta(days=7, seconds=5)).isoformat(),
    )
    assert result.data["entities"]["sensor.a"]["exists"] is False
    with pytest.raises(ServiceValidationError):
        await call_tool(
            "get_entity_history",
            entity_ids=["sensor.a"],
            start=(now - timedelta(days=7, hours=2)).isoformat(),
        )


class _Color(Enum):
    RED = "red"


def test_to_json_safe() -> None:
    """Everything found in states and YAML becomes a JSON primitive."""
    moment = dt_util.utc_from_timestamp(0)
    nested: Any = "leaf"
    for _ in range(40):
        nested = [nested]
    converted = to_json_safe(
        {
            "none": None,
            "enum": _Color.RED,
            "date": moment,
            "delta": timedelta(seconds=90),
            "set": {1},
            "tuple": (1, 2),
            "object": ValueError("boom"),
            1: "numeric key",
            "deep": nested,
        }
    )
    assert converted["none"] is None
    assert converted["enum"] == "red"
    assert converted["date"] == moment.isoformat()
    assert converted["delta"] == 90
    assert converted["set"] == [1]
    assert converted["tuple"] == [1, 2]
    assert converted["object"] == "boom"
    assert converted["1"] == "numeric key"
    json.dumps(converted)


async def test_get_system_info_gives_all_versions(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A bug report needs them, and what only some installations have is left out."""
    result = await call_tool("get_system_info")
    assert result.data["frontend_version"]
    assert "supervisor_version" not in result.data

    info = {
        "version": "1",
        "supervisor": "2026.10.0",
        "host_os": "Home Assistant OS 16.3",
        "docker_version": "28.3.3",
        "chassis": "vm",
        "user": "root",
    }
    with patch(
        "custom_components.llm_admin_tools.queries.system.async_get_system_info",
        return_value=info,
    ):
        result = await call_tool("get_system_info")
    assert result.data["supervisor_version"] == "2026.10.0"
    assert result.data["host_os"] == "Home Assistant OS 16.3"
    assert result.data["docker_version"] == "28.3.3"
    assert result.data["chassis"] == "vm"
    assert "container_arch" not in result.data
    assert "user" not in result.data


async def test_system_info_without_a_frontend_requirement(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """A frontend that states no version does not get in the way of the rest."""
    with patch(
        "custom_components.llm_admin_tools.queries.system.async_get_integration",
        return_value=MagicMock(requirements=["something==1"]),
    ):
        result = await call_tool("get_system_info")
    assert result.data["frontend_version"] is None
    assert result.data["version"]
