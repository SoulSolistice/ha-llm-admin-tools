"""Tests for what a change reaches: protected members, and the report on a removal."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_admin_tools.policy.limits import (
    MAX_REPORT_ITEMS,
    MAX_REPORT_USES,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.setup import async_setup_component

from .conftest import CallTool, apply_options

_REASON = "Asked for by the user in this test."


class _Family:
    """A config entry with a device, its child device and an entity on each."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.source = MockConfigEntry(domain="other", title="Other")
        self.source.add_to_hass(hass)
        devices = dr.async_get(hass)
        self.parent = devices.async_get_or_create(
            config_entry_id=self.source.entry_id,
            identifiers={("other", "parent")},
            name="Parent",
        )
        self.child = devices.async_get_or_create_child(
            config_entry_id=self.source.entry_id,
            parent_device_id=self.parent.id,
            identifiers={("other", "child")},
            name="Child",
        )
        entities = er.async_get(hass)
        self.on_parent = entities.async_get_or_create(
            "sensor",
            "other",
            "parent",
            suggested_object_id="one",
            device_id=self.parent.id,
            config_entry=self.source,
        )
        self.on_child = entities.async_get_or_create(
            "sensor",
            "other",
            "child",
            suggested_object_id="two",
            device_id=self.child.id,
            config_entry=self.source,
        )


@pytest.fixture
async def core(hass: HomeAssistant) -> None:
    """Have the commands of core available."""
    for component in ("websocket_api", "config", "lovelace"):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()


@pytest.fixture
def family(hass: HomeAssistant) -> _Family:
    """Return the things to remove."""
    return _Family(hass)


async def _refusal(call_tool: CallTool, tool: str, **args: Any) -> str | None:
    with pytest.raises(HomeAssistantError) as err:
        await call_tool(tool, reason=_REASON, dry_run=True, **args)
    return err.value.translation_key


@pytest.mark.usefixtures("core")
async def test_a_device_keeps_what_is_protected_below_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """Disabling, moving or deleting a parent reaches its child device as well."""
    area = ar.async_get(hass).async_create("Cellar")
    await apply_options(
        hass,
        init_integration,
        write_subjects=["devices"],
        write_allow_delete=True,
        protected_entities=[family.on_child.entity_id],
    )
    for args in (
        {"disabled_by": "user"},
        {"area_id": area.id},
        {"operation": "delete"},
    ):
        assert (
            await _refusal(
                call_tool, "update_device", device_id=family.parent.id, **args
            )
            == "in_use_by_protected"
        )


@pytest.mark.usefixtures("core")
async def test_a_device_keeps_a_protected_child_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """A protected label on the child device holds for the parent too."""
    label = lr.async_get(hass).async_create("Guarded")
    dr.async_get(hass).async_update_child_device(
        family.child.id, labels={label.label_id}
    )
    await apply_options(
        hass,
        init_integration,
        write_subjects=["devices"],
        protected_labels=[label.label_id],
    )
    assert (
        await _refusal(
            call_tool, "update_device", device_id=family.parent.id, disabled_by="user"
        )
        == "in_use_by_protected"
    )


@pytest.mark.usefixtures("core")
async def test_deleting_an_area_or_label_keeps_what_is_below_a_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """Without anything protected the same deletes pass."""
    area = ar.async_get(hass).async_create("Cellar")
    label = lr.async_get(hass).async_create("Tag")
    dr.async_get(hass).async_update_device(
        family.parent.id, area_id=area.id, labels={label.label_id}
    )
    options: dict[str, Any] = {
        "write_subjects": ["areas", "labels"],
        "write_allow_delete": True,
    }
    deletes: list[dict[str, str]] = [
        {"kind": "area", "item_id": area.id},
        {"kind": "label", "item_id": label.label_id},
    ]
    await apply_options(
        hass,
        init_integration,
        **options,
        protected_entities=[family.on_child.entity_id],
    )
    for args in deletes:
        assert (
            await _refusal(call_tool, "write_registry_item", operation="delete", **args)
            == "in_use_by_protected"
        )
    await apply_options(hass, init_integration, **options, protected_entities=[])
    for args in deletes:
        answer = await call_tool(
            "write_registry_item",
            operation="delete",
            reason=_REASON,
            dry_run=True,
            **args,
        )
        assert answer.data["dry_run"]


@pytest.mark.usefixtures("core")
async def test_a_config_entry_keeps_what_is_protected_or_hidden(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """Nothing may stop or start with it, while a reload leaves everything as it was."""
    subjects: dict[str, Any] = {
        "write_subjects": ["config_entries"],
        "write_allow_delete": True,
    }
    entry_id = family.source.entry_id
    for options in (
        {"protected_entities": [family.on_child.entity_id]},
        {"excluded_entities": [family.on_parent.entity_id]},
    ):
        await apply_options(hass, init_integration, **subjects, **options)
        for operation in ("delete", "disable", "enable"):
            assert (
                await _refusal(
                    call_tool,
                    "manage_config_entry",
                    entry_id=entry_id,
                    operation=operation,
                )
                == "in_use_by_protected"
            )
        answer = await call_tool(
            "manage_config_entry",
            entry_id=entry_id,
            operation="reload",
            reason=_REASON,
            dry_run=True,
        )
        assert "affects" not in answer.data


async def _report(call_tool: CallTool, family: _Family) -> dict[str, Any]:
    answer = await call_tool(
        "manage_config_entry",
        entry_id=family.source.entry_id,
        operation="delete",
        reason=_REASON,
        dry_run=True,
    )
    assert answer.data["dry_run"]
    report: dict[str, Any] = answer.data["affects"]
    return report


@pytest.mark.usefixtures("core")
async def test_deleting_an_integration_reports_what_uses_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """The report goes by the registries, as this integration is not even loaded."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                _automation("named", {"entity": "sensor.one", "number": 1}),
                _automation("in_template", {"a": "x\n{{ states.sensor.two.state }}"}),
                _automation("by_device", {"device": family.parent.id}),
                _automation("by_entry", {"entity": family.on_child.id}),
                _automation("other", {"entity": "sensor.one_more"}),
                _automation("hidden", {"entity": "sensor.one"}),
            ]
        },
    )
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "uses": {"sequence": [{"variables": {"entity": "SENSOR.ONE"}}]},
            }
        },
    )
    assert await async_setup_component(
        hass,
        "scene",
        {"scene": [{"name": "Uses", "entities": {"sensor.two": "on"}}]},
    )
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries", "dashboards"],
        write_allow_delete=True,
        excluded_entities=["automation.hidden"],
    )
    created = await call_tool(
        "write_dashboard",
        operation="create",
        fields={"url_path": "made-up", "title": "Made"},
        reason=_REASON,
    )
    assert created.data["done"]
    await call_tool(
        "write_dashboard",
        operation="save_config",
        url_path="made-up",
        config={"views": [{"cards": [{"type": "x", "entities": ["sensor.one"]}]}]},
        reason=_REASON,
    )

    report = await _report(call_tool, family)

    assert report["entities"] == {"count": 2, "items": ["sensor.one", "sensor.two"]}
    assert {device["name"] for device in report["devices"]["items"]} == {
        "Parent",
        "Child",
    }
    assert {
        (use["kind"], use["id"]): use["uses"] for use in report["used_by"]["items"]
    } == {
        ("automation", "automation.named"): ["sensor.one"],
        ("automation", "automation.in_template"): ["sensor.two"],
        ("automation", "automation.by_device"): [family.parent.id],
        ("automation", "automation.by_entry"): ["sensor.two"],
        ("dashboard", "made-up"): ["sensor.one"],
        ("scene", "scene.uses"): ["sensor.two"],
        ("script", "script.uses"): ["sensor.one"],
    }
    assert "not_checked" not in report
    assert "how_found" in report


def _automation(name: str, variables: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": name,
        "alias": name,
        "triggers": [{"trigger": "event", "event_type": "nothing"}],
        "actions": [{"variables": variables}],
    }


@pytest.mark.usefixtures("core")
async def test_report_reads_only_what_a_client_may_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """Tools the owner turned off keep what they read, and the report says so."""
    assert await async_setup_component(
        hass,
        "automation",
        {"automation": [_automation("named", {"entity": "sensor.one"})]},
    )
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
        enabled_tools=["get_system_info"],
    )
    report = await _report(call_tool, family)
    assert report["used_by"] == {"count": 0, "items": []}
    assert report["not_checked"] == ["automations, scripts and scenes", "dashboards"]


async def test_report_says_what_it_could_not_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """A report never stands in the way of what it reports on."""
    for component in ("websocket_api", "config"):
        assert await async_setup_component(hass, component, {})
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
    )
    assert (await _report(call_tool, family))["not_checked"] == ["dashboards"]


@pytest.mark.usefixtures("core")
async def test_report_says_a_dashboard_could_not_be_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """One that was never saved names nothing; one that fails might."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
    )

    async def fail(_hass: HomeAssistant, _user: str, command: str, *_: Any) -> Any:
        if command == "lovelace/dashboards/list":
            return []
        raise HomeAssistantError("it broke")

    with patch(
        "custom_components.llm_admin_tools.queries.references.async_run_command", fail
    ):
        assert (await _report(call_tool, family))["not_checked"] == ["dashboards"]


@pytest.mark.usefixtures("core")
async def test_report_states_how_many_when_not_all_fit(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """A big integration leaves room for the rest of the answer."""
    registry = er.async_get(hass)
    count = MAX_REPORT_ITEMS + 1
    ids = [
        registry.async_get_or_create(
            "sensor", "other", f"many{number}", config_entry=family.source
        ).entity_id
        for number in range(count)
    ]
    assert await async_setup_component(
        hass, "automation", {"automation": [_automation("all", {"entities": ids})]}
    )
    await hass.async_block_till_done()
    await apply_options(
        hass,
        init_integration,
        write_subjects=["config_entries"],
        write_allow_delete=True,
    )
    report = await _report(call_tool, family)
    assert report["entities"]["count"] == count + 2
    assert len(report["entities"]["items"]) == MAX_REPORT_ITEMS
    use = report["used_by"]["items"][0]
    assert use["uses_count"] == count
    assert len(use["uses"]) == MAX_REPORT_USES


@pytest.mark.usefixtures("core")
async def test_device_reports_on_what_goes_with_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    call_tool: CallTool,
    family: _Family,
) -> None:
    """Deleting and disabling report, changing anything else does not."""
    await apply_options(
        hass,
        init_integration,
        write_subjects=["devices"],
        write_allow_delete=True,
    )
    device_id = family.parent.id
    expected = {"count": 2, "items": ["sensor.one", "sensor.two"]}

    deleting = await call_tool(
        "update_device",
        device_id=device_id,
        operation="delete",
        reason=_REASON,
        dry_run=True,
    )
    assert deleting.data["affects"]["entities"] == expected
    renaming = await call_tool(
        "update_device",
        device_id=device_id,
        name_by_user="Other",
        disabled_by=None,
        reason=_REASON,
    )
    assert "affects" not in renaming.data

    # What it reports is from before the change.
    disabling = await call_tool(
        "update_device", device_id=device_id, disabled_by="user", reason=_REASON
    )
    assert disabling.data["done"]
    assert disabling.data["affects"]["entities"] == expected
    await hass.async_block_till_done()
    entry = er.async_get(hass).async_get(family.on_child.entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.DEVICE
