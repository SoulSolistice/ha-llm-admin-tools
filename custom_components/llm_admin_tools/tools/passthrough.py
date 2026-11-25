"""Tools of other LLM APIs, passed on through the checks of this integration."""

from collections import Counter
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, override

import probatio

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import slugify

from ..const import DOMAIN, LOGGER
from ..models import RuntimeData
from ..policy.credential_states import credential_values
from .base import GuardedTool
from .catalog import READ_TOOL_CLASSES, TOOL_CLASSES_FOR_CHANGES

# Not in the catalog, which it reports on.
CAPABILITIES_TOOL_NAME = "get_capabilities"
# A tool passed on never takes one of these, even while the owner has turned it off:
# its name would otherwise change when the owner turns that tool on.
RESERVED_NAMES = frozenset(
    {
        CAPABILITIES_TOOL_NAME,
        *(tool.name for tool in (*READ_TOOL_CLASSES, *TOOL_CLASSES_FOR_CHANGES)),
    }
)
RENAMED_PREFIX = "from_"

# The assistant the MCP server asks as.
_ASSISTANT = "conversation"
# Set while the tools of other APIs are gathered. An API that passes on this one
# would otherwise have it gather from that API again, without end.
_GATHERING: ContextVar[bool] = ContextVar(f"{DOMAIN}_gathering", default=False)


class PassthroughTool(GuardedTool):
    """A tool of another API, served with the checks every call here takes.

    Its interface and what it does are those of the tool it passes on.
    """

    def __init__(
        self, runtime: RuntimeData, api: llm.API, tool: llm.Tool, name: str
    ) -> None:
        """Initialize the tool."""
        super().__init__(runtime)
        self.api_id = api.id
        self.api_name = api.name
        self.tool = tool
        self.name = name
        self.title = tool.title
        self.description = tool.description
        self.parameters = tool.parameters
        self.annotations = tool.annotations
        self.integration = tool.integration or DOMAIN

    @override
    def _arguments(self, tool_args: dict[str, Any]) -> Mapping[str, Any]:
        """Leave the arguments to the tool, as Home Assistant does.

        Its schema is there to describe it and may not accept all the tool does.
        """
        return tool_args

    @override
    async def _async_answer(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> tuple[Mapping[str, Any], bool]:
        """Call the tool passed on, under the name it has in its own API."""
        result = await self.tool.async_call(
            hass,
            llm.ToolInput(tool_name=self.tool.name, tool_args=dict(args)),
            llm_context,
        )
        if isinstance(result, llm.ToolResult):
            return result.data, result.error
        return result, False


@dataclass(slots=True, kw_only=True)
class PassedOn:
    """What is passed on from other APIs for one request."""

    tools: list[PassthroughTool] = field(default_factory=list)
    # Instructions of the APIs, for the tools passed on from each.
    prompts: list[tuple[str, list[PassthroughTool], str]] = field(default_factory=list)
    serializers: list[Callable[[Any], Any]] = field(default_factory=list)

    def custom_serializer(self) -> Callable[[Any], Any] | None:
        """Return what describes the parameters of the tools passed on, if needed."""
        if not self.serializers:
            return None
        serializers = list(self.serializers)

        def serialize(schema: Any) -> Any:
            for serializer in serializers:
                result = serializer(schema)
                if result is not None and result is not probatio.UNSUPPORTED:
                    return result
            return probatio.UNSUPPORTED

        return serialize


def _served_here(tool: llm.Tool) -> bool:
    """Return whether a tool is one this integration serves, perhaps renamed."""
    while isinstance(tool, llm.NamespacedTool):
        tool = tool.tool
    return isinstance(tool, GuardedTool)


async def _async_other_instances(
    hass: HomeAssistant, llm_context: llm.LLMContext, wanted: Callable[[str], bool]
) -> list[llm.APIInstance]:
    """Return an instance of each other API wanted, leaving out any that fails.

    Called while gathering already, it returns none.
    """
    if _GATHERING.get():
        return []
    token = _GATHERING.set(True)
    instances: list[llm.APIInstance] = []
    try:
        for api in llm.async_get_apis(hass):
            if api.id == DOMAIN or not wanted(api.id):
                continue
            try:
                # Only an administrator gets here, which is all an API can require.
                instances.append(await api.async_get_api_instance(llm_context))
            except Exception as err:
                # One integration failing must not take the tools of this one along.
                LOGGER.warning(
                    "LLM API %s gave no tools to pass on: %s",
                    api.id,
                    type(err).__name__,
                )
                LOGGER.debug("Why LLM API %s gave no tools", api.id, exc_info=err)
    finally:
        _GATHERING.reset(token)
    return instances


async def async_passed_on(
    hass: HomeAssistant, runtime: RuntimeData, llm_context: llm.LLMContext
) -> PassedOn:
    """Return the tools of other APIs the owner chose, for an administrator."""
    settings = runtime.passthrough
    passed = PassedOn()
    if not settings.enabled:
        return passed
    chosen = [
        (
            instance,
            [
                tool
                for tool in instance.tools
                if not _served_here(tool)
                and settings.wants_tool(
                    instance.api.id,
                    tool.name,
                    integration=tool.integration,
                    read_only=tool.annotations.read_only,
                )
            ],
        )
        for instance in await _async_other_instances(
            hass, llm_context, settings.wants_api
        )
    ]
    # A name used twice gets the API. Not as Home Assistant does when it merges APIs:
    # merged with the API passed on, the two would give a tool the same name, and Home
    # Assistant refuses the merge. It names by the API's id, or its name before
    # 2026.11, slugified without an underscore, so what precedes __ here has one.
    counts = Counter(tool.name for _, tools in chosen for tool in tools)
    taken = set(RESERVED_NAMES)
    if settings.prompts:
        runtime.redactor.set_live_values(credential_values(hass))
    for instance, tools in chosen:
        api = instance.api
        wrapped = []
        for tool in tools:
            name = tool.name
            if name in taken or counts[name] > 1:
                name = f"{RENAMED_PREFIX}{slugify(api.id)}__{tool.name}"
            if name in taken:
                LOGGER.warning(
                    "Tool %s of LLM API %s is not passed on: its name is taken",
                    tool.name,
                    api.id,
                )
                continue
            taken.add(name)
            wrapped.append(PassthroughTool(runtime, api, tool, name))
        if not wrapped:
            continue
        passed.tools.extend(wrapped)
        if instance.custom_serializer is not None:
            passed.serializers.append(instance.custom_serializer)
        if settings.prompts and instance.api_prompt.strip():
            passed.prompts.append(
                (api.name, wrapped, runtime.redactor.scrub_text(instance.api_prompt))
            )
    return passed


async def async_offered_elsewhere(
    hass: HomeAssistant,
) -> list[tuple[llm.API, list[llm.Tool]]]:
    """Return the tools other APIs offer now, for the owner to choose from.

    What an API offers can depend on who asks and where. This asks as the MCP
    server does, without a user, as nothing is called.
    """
    llm_context = llm.LLMContext(
        platform=DOMAIN,
        context=Context(),
        language=hass.config.language,
        assistant=_ASSISTANT,
        device_id=None,
    )
    instances = await _async_other_instances(hass, llm_context, lambda _: True)
    return [
        (instance.api, [tool for tool in instance.tools if not _served_here(tool)])
        for instance in instances
    ]
