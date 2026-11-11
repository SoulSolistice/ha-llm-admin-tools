"""Tests for helpers computed from other entities, made through their forms."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownHandler
from homeassistant.exceptions import ServiceValidationError

from .conftest import CallTool, apply_options

_TOOL = "write_computed_helper"
_MODULE = "custom_components.llm_admin_tools.tools.write_computed_helpers"
_REASON = "Asked for by the user in this test."
_DERIVATIVE = {
    "name": "Warming",
    "source": "sensor.freezer",
    "round": 2,
    "time_window": {"minutes": 10},
    "unit_time": "h",
}


@pytest.fixture
async def helpers(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Allow helpers, and have a sensor to compute from."""
    hass.states.async_set("sensor.freezer", "-18.5", {"unit_of_measurement": "°C"})
    await apply_options(
        hass, init_integration, write_subjects=["helpers"], write_allow_delete=True
    )


async def _write(call_tool: CallTool, operation: str, **args: Any) -> dict[str, Any]:
    result = await call_tool(_TOOL, operation=operation, reason=_REASON, **args)
    return dict(result.data)


async def _refusal(call_tool: CallTool, operation: str, **args: Any) -> str | None:
    with pytest.raises(ServiceValidationError) as err:
        await _write(call_tool, operation, **args)
    return err.value.translation_key


def _entries(hass: HomeAssistant, domain: str) -> list[Any]:
    return hass.config_entries.async_entries(domain)


@pytest.mark.usefixtures("helpers")
async def test_derivative_life_cycle(hass: HomeAssistant, call_tool: CallTool) -> None:
    """A dry run makes nothing; then it is made, changed and deleted."""
    dry = await _write(
        call_tool, "create", kind="derivative", fields=_DERIVATIVE, dry_run=True
    )
    assert dry["dry_run"] is True
    assert dry["summary"].startswith("create derivative: ")
    assert not _entries(hass, "derivative")
    assert not hass.config_entries.flow.async_progress()

    created = await _write(call_tool, "create", kind="derivative", fields=_DERIVATIVE)
    await hass.async_block_till_done()
    (entry,) = _entries(hass, "derivative")
    assert created["result"]["entry_id"] == entry.entry_id
    assert created["result"]["title"] == "Warming"
    assert list(created["result"]["entities"]) == ["sensor.warming"]
    assert entry.options["source"] == "sensor.freezer"

    updated = await _write(
        call_tool, "update", entry_id=entry.entry_id, fields={"round": 1}
    )
    await hass.async_block_till_done()
    assert updated["before"]["round"] == 2
    assert entry.options["round"] == 1
    assert entry.options["time_window"] == {"minutes": 10}

    deleted = await _write(call_tool, "delete", entry_id=entry.entry_id, dry_run=True)
    assert "sensor.warming" in str(deleted["affects"])
    await _write(call_tool, "delete", entry_id=entry.entry_id)
    await hass.async_block_till_done()
    assert not _entries(hass, "derivative")


@pytest.mark.usefixtures("helpers")
async def test_a_form_in_several_steps(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Statistics asks in three forms, each for its own fields."""
    created = await _write(
        call_tool,
        "create",
        kind="statistics",
        fields={
            "name": "Freezer mean",
            "entity_id": "sensor.freezer",
            "state_characteristic": "mean",
            "max_age": {"hours": 1},
        },
    )
    await hass.async_block_till_done()
    (entry,) = _entries(hass, "statistics")
    assert created["result"]["entry_id"] == entry.entry_id
    assert entry.options["state_characteristic"] == "mean"


@pytest.mark.usefixtures("helpers")
async def test_what_it_reads_must_be_seen(
    hass: HomeAssistant, init_integration: MockConfigEntry, call_tool: CallTool
) -> None:
    """A helper would show what it reads, so a hidden entity looks as if not there."""
    warned = await _write(
        call_tool,
        "create",
        kind="min_max",
        fields={
            "name": "M",
            "entity_ids": ["sensor.freezer", "sensor.typo"],
            "type": "max",
        },
        dry_run=True,
    )
    assert "sensor.typo." in warned["warnings"][0]

    hass.states.async_set("camera.c", "idle")
    await apply_options(
        hass,
        init_integration,
        write_subjects=["helpers"],
        excluded_domains=["camera"],
    )
    for source in ("camera.c", "sensor.typo"):
        assert (
            await _refusal(
                call_tool,
                "create",
                kind="derivative",
                fields={**_DERIVATIVE, "source": source},
                dry_run=True,
            )
            == "entity_not_found"
        )
    assert not _entries(hass, "derivative")


@pytest.mark.usefixtures("helpers")
async def test_fields_its_form_refuses(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Refused before anything is made, and no form is left open."""
    assert (
        await _refusal(
            call_tool,
            "create",
            kind="derivative",
            fields={**_DERIVATIVE, "round": "many"},
            dry_run=True,
        )
        == "computed_helper_fields_invalid"
    )
    # Threshold needs a lower or upper bound, which its form says once filled in.
    assert (
        await _refusal(
            call_tool,
            "create",
            kind="threshold",
            fields={"name": "T", "entity_id": "sensor.freezer", "hysteresis": 0},
        )
        == "computed_helper_fields_invalid"
    )
    # A later form is only checked when it comes.
    assert (
        await _refusal(
            call_tool,
            "create",
            kind="statistics",
            fields={
                "name": "S",
                "entity_id": "sensor.freezer",
                "state_characteristic": "nonsense",
            },
        )
        == "computed_helper_fields_invalid"
    )
    assert not hass.config_entries.flow.async_progress()
    assert not _entries(hass, "threshold")
    assert not _entries(hass, "statistics")


@pytest.mark.usefixtures("helpers")
async def test_what_it_needs_and_what_it_refuses(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Only these kinds, and each operation with what it takes."""
    other = MockConfigEntry(domain="template")
    other.add_to_hass(hass)
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("create", {"kind": "derivative"}, "computed_helper_fields_required"),
        ("create", {"fields": _DERIVATIVE}, "computed_helper_kind_required"),
        ("update", {"fields": {}}, "registry_item_id_required"),
        ("delete", {"entry_id": "nope"}, "config_entry_not_found"),
        ("delete", {"entry_id": other.entry_id}, "computed_helper_kind"),
    ]
    for operation, args, key in cases:
        assert await _refusal(call_tool, operation, **args) == key


@pytest.mark.usefixtures("helpers")
async def test_when_the_integration_offers_no_form(
    hass: HomeAssistant, call_tool: CallTool
) -> None:
    """Whatever it does instead, nothing is made."""
    flow = hass.config_entries.flow
    answers: list[Any] = [
        UnknownHandler(),
        {"type": FlowResultType.ABORT, "flow_id": "a", "reason": "single"},
        {"type": FlowResultType.EXTERNAL_STEP, "flow_id": "b"},
    ]
    for answer in answers:
        with patch.object(flow, "async_init", side_effect=[answer]):
            assert (
                await _refusal(
                    call_tool, "create", kind="trend", fields={}, dry_run=True
                )
                == "computed_helper_refused"
            )
    with patch(f"{_MODULE}._MAX_STEPS", 1):
        assert (
            await _refusal(
                call_tool,
                "create",
                kind="statistics",
                fields={"name": "S", "entity_id": "sensor.freezer"},
            )
            == "computed_helper_refused"
        )
    assert not flow.async_progress()
