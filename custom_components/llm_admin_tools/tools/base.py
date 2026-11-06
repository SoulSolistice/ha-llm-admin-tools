"""The one path every tool call takes through the policy."""

from abc import abstractmethod
from collections.abc import Mapping
import json
from typing import Any, ClassVar, final, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..const import DOMAIN, LOGGER
from ..errors import read_failed
from ..models import RuntimeData
from ..policy.access import async_require_admin
from ..policy.exposure import ExposurePolicy
from ..policy.limits import MAX_RESULT_CHARACTERS
from ..serialization import to_json_safe

READ_ONLY_ANNOTATIONS = llm.ToolAnnotations(
    read_only=True, destructive=False, idempotent=True, open_world=False
)


class BaseTool(llm.Tool):
    """A tool; subclasses state its interface and what it does.

    ``async_call`` is final so that no tool can skip the admin check, argument
    validation, redaction, the size limit or the audit record.
    """

    integration = DOMAIN
    max_result_characters: ClassVar[int] = MAX_RESULT_CHARACTERS

    def __init__(self, runtime: RuntimeData) -> None:
        """Initialize the tool."""
        self._runtime = runtime

    @property
    def exposure(self) -> ExposurePolicy:
        """Return what the settings let this tool show."""
        return self._runtime.exposure

    @classmethod
    def unavailable_because(
        cls, hass: HomeAssistant, runtime: RuntimeData
    ) -> str | None:
        """Return why this cannot work right now, for get_capabilities to say."""
        return None

    @classmethod
    def limited_because(cls, runtime: RuntimeData) -> str | None:
        """Return what this cannot do right now, for get_capabilities to say."""
        return None

    @abstractmethod
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Return the data for validated arguments."""

    def reduce_result(
        self, result: dict[str, Any], args: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return a redacted result with what the client does not need removed.

        This runs after redaction: text cut short first could leave part of a secret
        that redaction no longer recognizes.
        """
        return result

    @final
    def _prepare_result(
        self, data: Mapping[str, Any], args: Mapping[str, Any]
    ) -> JsonObjectType:
        """Return what may leave; runs in an executor, as a long result takes time."""
        result: JsonObjectType = self.reduce_result(
            self._runtime.redactor.redact(to_json_safe(data)), args
        )
        if len(json.dumps(result, ensure_ascii=False)) > self.max_result_characters:
            raise read_failed("result_too_large")
        return result

    @final
    def _stated_reason(self, args: Mapping[str, Any]) -> str | None:
        """Return the reason a client gave, on one line and with credentials removed."""
        if "reason" not in args:
            return None
        return self._runtime.redactor.scrub_text(" ".join(args["reason"].split()))

    @final
    def _require_current(self, hass: HomeAssistant) -> None:
        """Raise if these tools outlived the settings they were built with.

        Whoever keeps a tool between calls would otherwise keep what it could do then,
        after the owner took that away, and even after the integration was unloaded.
        """
        if not any(
            entry.runtime_data is self._runtime
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        ):
            raise read_failed("tools_outdated")

    @final
    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Check access and arguments, perform, and return the redacted result."""
        success = False
        reason: str | None = None
        try:
            await async_require_admin(hass, llm_context)
            self._require_current(hass)
            self._runtime.audit.require_capacity()
            # Home Assistant passes the arguments on without validating them.
            args = self.parameters(tool_input.tool_args)
            reason = self._stated_reason(args)
            data = await self.async_perform(hass, args, llm_context)
            result = await hass.async_add_executor_job(self._prepare_result, data, args)
            success = True
            return llm.ToolResult(data=result)
        except (HomeAssistantError, probatio.Invalid) as err:
            # These say why a call failed, which a client needs, but can quote anything.
            message = str(err)
            scrubbed = self._runtime.redactor.scrub_text(message)
            if scrubbed == message:
                raise
            raise HomeAssistantError(scrubbed) from None
        except Exception as err:
            # Anything else is not meant to be read by a client, and says nothing it can
            # act on. A client can read the log too, so the text only goes there if the
            # owner has debug logging on.
            kind = type(err).__name__
            LOGGER.warning("Tool %s failed with %s", self.name, kind)
            LOGGER.debug("What failed in tool %s", self.name, exc_info=err)
            raise read_failed("tool_failed", kind=kind) from None
        finally:
            user_id = llm_context.context.user_id if llm_context.context else None
            self._runtime.audit.record(self.name, user_id, success, reason)


class ReadOnlyTool(BaseTool):
    """A tool that reads what it can get at without acting for the caller."""

    annotations = READ_ONLY_ANNOTATIONS

    @abstractmethod
    async def async_read(
        self, hass: HomeAssistant, args: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return the data for validated arguments."""

    @final
    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Read."""
        return await self.async_read(hass, args)
