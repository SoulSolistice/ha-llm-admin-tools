"""Tests for passing on the tools of other LLM APIs."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, override

import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.llm_admin_tools.const import (
    CONF_PASSTHROUGH_APIS,
    CONF_PASSTHROUGH_PROMPTS,
    CONF_PASSTHROUGH_READ_ONLY,
    CONF_PASSTHROUGH_TOOLS,
    DOMAIN,
)
from custom_components.llm_admin_tools.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.llm_admin_tools.policy.redaction import REDACTED
from custom_components.llm_admin_tools.policy.settings import PassthroughSettings
from custom_components.llm_admin_tools.tools import TOOL_CLASSES
from custom_components.llm_admin_tools.tools.base import READ_ONLY_ANNOTATIONS
from custom_components.llm_admin_tools.tools.capabilities import GetCapabilitiesTool
from custom_components.llm_admin_tools.tools.passthrough import (
    RESERVED_NAMES,
    PassedOn,
    PassthroughTool,
)
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from .conftest import SECRET_VALUE, CallTool, apply_options, make_llm_context

_OWN = {tool_class.name for tool_class in TOOL_CLASSES}


class FakeTool(llm.Tool):
    """A tool of another integration that records how it was called."""

    def __init__(
        self,
        name: str,
        *,
        read_only: bool = False,
        result: llm.ToolResult | dict[str, Any] | None = None,
        error: Exception | None = None,
        integration: str | None = None,
    ) -> None:
        """Initialize the tool."""
        self.name = name
        self.title = f"Fake {name}"
        self.description = f"Does {name}."
        self.parameters = probatio.Schema({probatio.Optional("value"): str})
        self.annotations = READ_ONLY_ANNOTATIONS if read_only else llm.ToolAnnotations()
        self.integration = integration
        self.result = result if result is not None else {"ok": True}
        self.error = error
        self.calls: list[llm.ToolInput] = []

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult | dict[str, Any]:
        """Record the call and answer as told."""
        self.calls.append(tool_input)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(kw_only=True)
class FakeAPI(llm.API):
    """An LLM API of another integration."""

    tools: list[llm.Tool] = field(default_factory=list)
    prompt: str = "Use the fake tools kindly."
    serializer: Callable[[Any], Any] | None = None
    fails: bool = False

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the tools, or fail if told to."""
        if self.fails:
            raise RuntimeError("broken")
        return llm.APIInstance(
            api=self,
            api_prompt=self.prompt,
            llm_context=llm_context,
            tools=list(self.tools),
            custom_serializer=self.serializer,
        )


@dataclass(kw_only=True)
class WrappingAPI(llm.API):
    """An API that passes on this integration's API, as this one passes on others."""

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the tools of this integration, renamed, and one of its own."""
        inner = await llm.async_get_api(self.hass, DOMAIN, llm_context)
        return llm.APIInstance(
            api=self,
            api_prompt="",
            llm_context=llm_context,
            tools=[
                *(llm.NamespacedTool("wrap", tool) for tool in inner.tools),
                FakeTool("wrapped_own", read_only=True),
            ],
        )


@contextmanager
def _registered(hass: HomeAssistant, api: llm.API) -> Generator[None]:
    unregister = llm.async_register_api(hass, api)
    try:
        yield
    finally:
        unregister()


@pytest.fixture
def fake_api(hass: HomeAssistant) -> Generator[FakeAPI]:
    """Register an API with a tool that reads, one that writes, and a clash."""
    api = FakeAPI(
        hass=hass,
        id="fake",
        name="Fake API",
        tools=[
            FakeTool(
                "peek",
                read_only=True,
                result={"value": SECRET_VALUE, "api_key": "abc", "plain": "fine"},
                integration="fake_domain",
            ),
            FakeTool("poke"),
            FakeTool("get_entity", read_only=True),
        ],
    )
    with _registered(hass, api):
        yield api


def _tool(api: FakeAPI, name: str) -> FakeTool:
    tool = next(tool for tool in api.tools if tool.name == name)
    assert isinstance(tool, FakeTool)
    return tool


async def _instance(hass: HomeAssistant, context: llm.LLMContext) -> llm.APIInstance:
    return await llm.async_get_api(hass, DOMAIN, context)


def _passed(instance: llm.APIInstance) -> dict[str, PassthroughTool]:
    return {
        tool.name: tool for tool in instance.tools if isinstance(tool, PassthroughTool)
    }


async def test_nothing_is_passed_on_until_chosen(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """Other APIs are not even asked for their tools."""
    fake_api.fails = True
    instance = await _instance(hass, admin_context)
    assert {tool.name for tool in instance.tools} == _OWN
    assert "other integrations" not in instance.api_prompt
    assert instance.custom_serializer is None


async def test_all_tools_of_an_api_that_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """Only tools that read, by default; a name of this integration's gets the API."""
    await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["fake"]})
    instance = await _instance(hass, admin_context)
    passed = _passed(instance)
    assert set(passed) == {"peek", "fake__get_entity"}
    assert {tool.name for tool in instance.tools} == _OWN | set(passed)
    peek = passed["peek"]
    assert peek.api_name == "Fake API"
    assert peek.title == "Fake peek"
    assert peek.description == "Does peek."
    assert peek.parameters is _tool(fake_api, "peek").parameters
    assert peek.annotations.read_only
    assert peek.integration == "fake_domain"
    # A tool that names no integration is served by this one.
    assert passed["fake__get_entity"].integration == DOMAIN
    assert (
        "The tools peek, fake__get_entity come from other integrations"
        in instance.api_prompt
    )
    assert "Instructions from" not in instance.api_prompt


async def test_tools_that_change_things_once_allowed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """A tool that declares nothing counts as one that can change things."""
    await apply_options(
        hass,
        init_integration,
        **{CONF_PASSTHROUGH_APIS: ["fake"], CONF_PASSTHROUGH_READ_ONLY: False},
    )
    passed = _passed(await _instance(hass, admin_context))
    assert set(passed) == {"peek", "poke", "fake__get_entity"}
    assert not passed["poke"].annotations.read_only


@pytest.mark.parametrize(("read_only", "expected"), [(True, set()), (False, {"poke"})])
async def test_single_tools(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
    *,
    read_only: bool,
    expected: set[str],
) -> None:
    """One tool chosen is passed on alone, unless it changes things and may not."""
    await apply_options(
        hass,
        init_integration,
        **{
            CONF_PASSTHROUGH_TOOLS: ["fake/poke"],
            CONF_PASSTHROUGH_READ_ONLY: read_only,
        },
    )
    assert set(_passed(await _instance(hass, admin_context))) == expected


async def test_call_is_passed_on_and_redacted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    call_tool: CallTool,
) -> None:
    """The arguments reach the tool as given, under its own name; the result not."""
    await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["fake"]})
    result = await call_tool("peek", anything=1, reason=5)
    assert result.data == {"value": REDACTED, "api_key": REDACTED, "plain": "fine"}
    assert not result.error
    call = _tool(fake_api, "peek").calls[-1]
    assert call.tool_name == "peek"
    assert call.tool_args == {"anything": 1, "reason": 5}
    last = init_integration.runtime_data.audit.as_diagnostics()[-1]
    assert last == {"timestamp": last["timestamp"], "tool": "peek", "success": True}

    await call_tool("fake__get_entity", reason="  to check\nthis ")
    assert _tool(fake_api, "get_entity").calls[-1].tool_name == "get_entity"
    last = init_integration.runtime_data.audit.as_diagnostics()[-1]
    assert last["tool"] == "fake__get_entity"
    assert last["reason"] == "to check this"


async def test_failure_reported_by_a_tool_is_kept(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    call_tool: CallTool,
) -> None:
    """A tool can answer that it failed instead of raising."""
    _tool(fake_api, "peek").result = llm.ToolResult(
        data={"error": SECRET_VALUE}, error=True
    )
    await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["fake"]})
    result = await call_tool("peek")
    assert result.error
    assert result.data == {"error": REDACTED}
    assert init_integration.runtime_data.audit.as_diagnostics()[-1]["success"] is False


async def test_errors_are_scrubbed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    call_tool: CallTool,
) -> None:
    """An error quotes nothing withheld, and an unexpected one says nothing."""
    peek = _tool(fake_api, "peek")
    await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["fake"]})
    peek.error = HomeAssistantError(f"Failed with {SECRET_VALUE}")
    with pytest.raises(HomeAssistantError) as err:
        await call_tool("peek")
    assert SECRET_VALUE not in str(err.value)
    assert REDACTED in str(err.value)

    peek.error = RuntimeError(SECRET_VALUE)
    with pytest.raises(HomeAssistantError) as err:
        await call_tool("peek")
    assert err.value.translation_key == "tool_failed"


async def test_only_administrators(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
    hass_read_only_user: User,
) -> None:
    """A non-admin gets none, and a tool kept from an admin's instance refuses."""
    await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["fake"]})
    peek = _passed(await _instance(hass, admin_context))["peek"]
    context = make_llm_context(hass_read_only_user.id)
    api = next(api for api in llm.async_get_apis(hass) if api.id == DOMAIN)
    assert (await api.async_get_api_instance(context)).tools == []
    with pytest.raises(Unauthorized):
        await peek.async_call(
            hass, llm.ToolInput(tool_name="peek", tool_args={}), context
        )
    assert _tool(fake_api, "peek").calls == []


async def test_kept_tool_refuses_after_the_settings_changed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """The owner taking a tool away also stops one a client kept."""
    await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["fake"]})
    peek = _passed(await _instance(hass, admin_context))["peek"]
    await apply_options(hass, init_integration)
    with pytest.raises(HomeAssistantError) as err:
        await peek.async_call(
            hass, llm.ToolInput(tool_name="peek", tool_args={}), admin_context
        )
    assert err.value.translation_key == "tools_outdated"


async def test_failing_api_is_left_out(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One integration failing does not take the tools of another along."""
    broken = FakeAPI(hass=hass, id="broken", name="Broken", fails=True)
    with _registered(hass, broken):
        await apply_options(
            hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["broken", "fake"]}
        )
        instance = await _instance(hass, admin_context)
    assert "peek" in _passed(instance)
    assert "LLM API broken gave no tools to pass on: RuntimeError" in caplog.text


async def test_api_that_passes_on_this_one_does_not_go_round(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
) -> None:
    """The tools of this integration are not passed on to itself."""
    with _registered(hass, WrappingAPI(hass=hass, id="wrap", name="Wrap")):
        await apply_options(hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["wrap"]})
        instance = await _instance(hass, admin_context)
    assert set(_passed(instance)) == {"wrapped_own"}
    assert len(instance.tools) == len(_OWN) + 1


async def test_name_used_twice_gets_the_api(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    admin_context: llm.LLMContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As when Home Assistant merges APIs; a name still taken then is left out."""
    first = FakeAPI(
        hass=hass,
        id="first",
        name="First",
        tools=[
            FakeTool("first__same", read_only=True),
            FakeTool("same", read_only=True),
        ],
    )
    second = FakeAPI(
        hass=hass, id="second", name="Second", tools=[FakeTool("same", read_only=True)]
    )
    with _registered(hass, first), _registered(hass, second):
        await apply_options(
            hass, init_integration, **{CONF_PASSTHROUGH_APIS: ["first", "second"]}
        )
        passed = _passed(await _instance(hass, admin_context))
    assert set(passed) == {"first__same", "second__same"}
    assert passed["first__same"].tool is first.tools[0]
    assert "Tool same of LLM API first is not passed on" in caplog.text


async def test_instructions_are_passed_on_redacted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """Each API's prompt comes with the names its tools have here and there."""
    fake_api.prompt = f"Mind the code {SECRET_VALUE}."
    quiet = FakeAPI(
        hass=hass, id="quiet", name="Quiet", prompt=" ", tools=[FakeTool("hush")]
    )
    with _registered(hass, quiet):
        await apply_options(
            hass,
            init_integration,
            **{
                CONF_PASSTHROUGH_APIS: ["fake", "quiet"],
                CONF_PASSTHROUGH_READ_ONLY: False,
                CONF_PASSTHROUGH_PROMPTS: True,
            },
        )
        prompt = (await _instance(hass, admin_context)).api_prompt
    assert (
        'Instructions from "Fake API" for peek, poke, fake__get_entity '
        f"(there get_entity):\nMind the code {REDACTED}."
    ) in prompt
    assert SECRET_VALUE not in prompt
    assert "hush" in prompt
    assert 'Instructions from "Quiet"' not in prompt


async def test_parameters_are_described_by_their_api(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """Each serializer is asked in turn, of the APIs that tools are passed on from."""
    fake_api.serializer = lambda schema: (
        {"a": 1} if schema == "a" else probatio.UNSUPPORTED
    )
    unused = FakeAPI(
        hass=hass,
        id="unused",
        name="Unused",
        tools=[FakeTool("unused")],
        serializer=lambda schema: {"unused": 1},
    )
    other = FakeAPI(
        hass=hass,
        id="other",
        name="Other",
        tools=[FakeTool("other", read_only=True)],
        serializer=lambda schema: {"b": 1} if schema == "b" else None,
    )
    with _registered(hass, unused), _registered(hass, other):
        await apply_options(
            hass,
            init_integration,
            **{CONF_PASSTHROUGH_APIS: ["fake", "unused", "other"]},
        )
        serializer = (await _instance(hass, admin_context)).custom_serializer
    assert serializer is not None
    assert serializer("a") == {"a": 1}
    assert serializer("b") == {"b": 1}
    assert serializer("c") is probatio.UNSUPPORTED
    assert PassedOn().custom_serializer() is None


async def test_capabilities_list_what_is_passed_on(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    call_tool: CallTool,
) -> None:
    """A client learns which tools come from elsewhere and what covers them."""
    report = (await call_tool("get_capabilities")).data
    assert report["other_tools"] == {"passed_on": []}
    await apply_options(
        hass,
        init_integration,
        **{CONF_PASSTHROUGH_APIS: ["fake"], CONF_PASSTHROUGH_READ_ONLY: False},
    )
    report = (await call_tool("get_capabilities", sections=["other_tools"])).data
    assert set(report) == {"current_time", "notes", "other_tools"}
    assert report["other_tools"]["passed_on"] == [
        {"name": "peek", "from": "Fake API", "can_change_things": False},
        {"name": "poke", "from": "Fake API", "can_change_things": True},
        {"name": "fake__get_entity", "from": "Fake API", "can_change_things": False},
    ]
    assert "redaction" in report["other_tools"]["rules"]
    report = (await call_tool("get_capabilities", sections=["writes"])).data
    assert "other_tools" not in report


async def test_diagnostics_show_the_choice(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The APIs and tools chosen name nothing in the home."""
    await apply_options(
        hass,
        init_integration,
        **{CONF_PASSTHROUGH_APIS: ["b", "a"], CONF_PASSTHROUGH_TOOLS: ["c/x"]},
    )
    data = await async_get_config_entry_diagnostics(hass, init_integration)
    assert data["passthrough"] == {
        "apis": ["a", "b"],
        "tools": ["c/x"],
        "read_only": True,
        "prompts": False,
    }


def test_reserved_names_cover_this_integration() -> None:
    """A tool passed on never takes the name of one of this integration's."""
    assert _OWN <= RESERVED_NAMES
    assert GetCapabilitiesTool.name in RESERVED_NAMES


def test_settings_start_with_nothing() -> None:
    """Nothing is passed on, and only tools that read once something is."""
    settings = PassthroughSettings.from_options({})
    assert not settings.enabled
    assert settings.read_only
    assert not settings.prompts
    chosen = PassthroughSettings.from_options({CONF_PASSTHROUGH_TOOLS: ["a/b/c"]})
    assert chosen.wants_api("a/b")
    assert not chosen.wants_api("a")
    assert chosen.wants_tool("a/b", "c", read_only=True)
    assert not chosen.wants_tool("a/b", "c", read_only=False)


async def _open_other_tools(hass: HomeAssistant, entry: MockConfigEntry) -> FlowResult:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "other_tools"}
    )
    assert result["type"] is FlowResultType.FORM
    return result


def _options(result: FlowResult, key: str) -> list[dict[str, str]]:
    for marker, selector in result["data_schema"].schema.items():
        if marker == key:
            return list(selector.config["options"])
    raise AssertionError(f"{key} is not in the form")


async def test_form_offers_the_other_apis_and_their_tools(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_api: FakeAPI
) -> None:
    """Neither this integration nor one that fails is among them."""
    broken = FakeAPI(hass=hass, id="broken", name="Broken", fails=True)
    with _registered(hass, broken):
        result = await _open_other_tools(hass, init_integration)
    assert _options(result, CONF_PASSTHROUGH_APIS) == [
        {"value": "fake", "label": "Fake API (fake)"}
    ]
    assert _options(result, CONF_PASSTHROUGH_TOOLS) == [
        {"value": "fake/peek", "label": "Fake API: peek"},
        {"value": "fake/poke", "label": "Fake API: poke (can change things)"},
        {"value": "fake/get_entity", "label": "Fake API: get_entity"},
    ]


async def test_form_saves_and_applies(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_api: FakeAPI,
    admin_context: llm.LLMContext,
) -> None:
    """A tool that changes things needs Only tools that read off."""
    user_input = {
        CONF_PASSTHROUGH_APIS: [],
        CONF_PASSTHROUGH_TOOLS: ["fake/poke"],
        CONF_PASSTHROUGH_READ_ONLY: True,
        CONF_PASSTHROUGH_PROMPTS: False,
    }
    result = await _open_other_tools(hass, init_integration)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["errors"] == {CONF_PASSTHROUGH_TOOLS: "passthrough_not_read_only"}

    user_input[CONF_PASSTHROUGH_READ_ONLY] = False
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == user_input
    assert set(_passed(await _instance(hass, admin_context))) == {"poke"}


async def test_form_keeps_what_is_gone_for_now(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An API not loaded at the moment does not block saving, nor get lost."""
    await apply_options(
        hass,
        init_integration,
        **{CONF_PASSTHROUGH_APIS: ["gone"], CONF_PASSTHROUGH_TOOLS: ["gone/x"]},
    )
    result = await _open_other_tools(hass, init_integration)
    assert {"value": "gone", "label": "gone"} in _options(result, CONF_PASSTHROUGH_APIS)
    assert {"value": "gone/x", "label": "gone/x"} in _options(
        result, CONF_PASSTHROUGH_TOOLS
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PASSTHROUGH_APIS: ["gone"],
            CONF_PASSTHROUGH_TOOLS: ["gone/x"],
            CONF_PASSTHROUGH_READ_ONLY: True,
            CONF_PASSTHROUGH_PROMPTS: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_assist_through_the_mcp_server(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Assist's tools reach a client with their parameters described."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "intent", {})
    assert await async_setup_component(hass, "llm", {})
    entry = MockConfigEntry(
        domain="mcp_server",
        data={
            "all_llm_apis": True,
            "llm_hass_api": [],
            "require_admin": True,
            "read_only": False,
        },
        minor_version=99,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await apply_options(
        hass,
        init_integration,
        **{
            CONF_PASSTHROUGH_APIS: [llm.LLM_API_ASSIST],
            CONF_PASSTHROUGH_READ_ONLY: False,
        },
    )
    client = await hass_client()
    response = await client.post(
        f"/api/mcp/{DOMAIN}",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status == HTTPStatus.OK
    tools = {tool["name"]: tool for tool in (await response.json())["result"]["tools"]}
    # Assist names each tool after the integration that provides it.
    turn_on = tools["intent__HassTurnOn"]
    assert turn_on["inputSchema"]["type"] == "object"
    assert turn_on["inputSchema"]["properties"]["name"] == {"type": "string"}
    assert not turn_on["annotations"]["readOnlyHint"]
