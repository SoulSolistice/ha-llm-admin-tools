"""What every tool that changes something has in common."""

from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
import json
from typing import Any, ClassVar, final, override

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ..core_commands import async_run_command, check_command
from ..errors import invalid_input
from ..policy.limits import (
    MAX_CHANGE_SUMMARY_CHARACTERS,
    MAX_TEXT_FILTER_LENGTH,
    MIN_REASON_LENGTH,
)
from ..policy.members import Members
from ..policy.redaction import REDACTED
from ..policy.writes import WritePolicy
from ..queries.impact import async_removal_report
from .base import BaseTool
from .references import readable_sources

WRITE_ANNOTATIONS = llm.ToolAnnotations(
    read_only=False, destructive=True, idempotent=False, open_world=False
)

WRITE_PARAMS: dict[Any, Any] = {
    probatio.Required(
        "reason",
        description="Why this change is made, for the owner to read.",
    ): probatio.All(
        str, probatio.Length(min=MIN_REASON_LENGTH, max=MAX_TEXT_FILTER_LENGTH)
    ),
    probatio.Optional(
        "dry_run",
        description="Check everything and report what would happen, without doing it.",
        default=False,
    ): bool,
}
DELETE_OPERATIONS = frozenset({"delete", "delete_config"})


@dataclass(slots=True, frozen=True, kw_only=True)
class WritePlan:
    """A change that passed every check and has not been made yet."""

    # One line for the owner, naming what changes. It goes into the journal.
    summary: str
    apply: Callable[[], Awaitable[Any]]
    # What is there now, so that a client can undo the change.
    before: Any = None
    # Reports what would go and what uses it, for what cannot be undone that easily.
    affects: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # What is allowed, but may well not be what the client meant.
    warnings: tuple[str, ...] = ()


def change_summary(change: str, fields: Mapping[str, Any]) -> str:
    """Return what the journal says of a change: what, and which fields are set."""
    summary = f"{change}: {', '.join(sorted(fields))}" if fields else change
    return summary[:MAX_CHANGE_SUMMARY_CHARACTERS]


def _holds_redacted_value(value: Any) -> bool:
    """Return if a value holds what a client got instead of a withheld value."""
    if isinstance(value, str):
        return REDACTED in value
    if isinstance(value, Mapping):
        # Redaction scrubs keys as well, so one can come back as the marker too.
        return any(
            _holds_redacted_value(item) for pair in value.items() for item in pair
        )
    if isinstance(value, list):
        return any(_holds_redacted_value(item) for item in value)
    return False


class WriteTool(BaseTool):
    """A tool that changes something; subclasses state how to plan the change.

    All checks happen while planning, so a dry run is the same call up to the point
    where the change would be made.
    """

    annotations = WRITE_ANNOTATIONS
    # What the owner must enable; a tool is offered if any of these is enabled.
    subjects: ClassVar[tuple[str, ...]]

    @property
    def writes(self) -> WritePolicy:
        """Return what the settings let this tool change."""
        return self._runtime.writes

    def subject_of(self, args: Mapping[str, Any]) -> str:
        """Return what the owner must have enabled for this call."""
        return self.subjects[0]

    def core_command(
        self,
        hass: HomeAssistant,
        llm_context: llm.LLMContext,
        command: str,
        fields: dict[str, Any],
    ) -> Callable[[], Awaitable[Any]]:
        """Return how to run a command of core, whose schema is checked right now."""
        check_command(hass, command, fields)
        user_id = llm_context.context.user_id if llm_context.context else None
        return partial(async_run_command, hass, user_id, command, fields)

    def removal_report(
        self, hass: HomeAssistant, llm_context: llm.LLMContext, members: Members
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        """Return how to report on a removal, reading only what a client may read."""
        return partial(
            async_removal_report,
            hass,
            self.exposure,
            llm_context.context.user_id if llm_context.context else None,
            members,
            **readable_sources(self.exposure),
        )

    @override
    def reduce_result(
        self, result: dict[str, Any], args: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Leave out what does not fit, instead of failing after making a change.

        A client told that a change failed would make it again.
        """
        for key in ("before", "affects", "result"):
            size = len(json.dumps(result.get(key), ensure_ascii=False))
            if size > self.max_result_characters // 4:
                del result[key]
                result[f"{key}_left_out"] = True
        return result

    @abstractmethod
    async def async_plan(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> WritePlan:
        """Check everything about a change and return how to make it."""

    @final
    @override
    async def async_perform(
        self, hass: HomeAssistant, args: Mapping[str, Any], llm_context: llm.LLMContext
    ) -> Mapping[str, Any]:
        """Check and plan a change, and make it unless this is a dry run."""
        self.writes.require_subject(self.subject_of(args))
        if args.get("operation") in DELETE_OPERATIONS:
            self.writes.require_delete()
        # Saved as is, this would replace what was withheld with the marker for good.
        # A reason is not saved there, and may well quote what a client was shown.
        stated = {key: value for key, value in args.items() if key != "reason"}
        if _holds_redacted_value(stated):
            raise invalid_input("redacted_value", marker=REDACTED)

        plan = await self.async_plan(hass, args, llm_context)
        outcome: dict[str, Any] = {
            "summary": plan.summary,
            **({"before": plan.before} if plan.before is not None else {}),
            # Once it is done, there is nothing left to report on.
            **({"affects": await plan.affects()} if plan.affects is not None else {}),
            **({"warnings": list(plan.warnings)} if plan.warnings else {}),
        }
        if args["dry_run"]:
            return {"dry_run": True, **outcome}

        self._runtime.journal.require_capacity()
        context = llm_context.context
        success = False
        try:
            result = await plan.apply()
            success = True
        finally:
            self._runtime.journal.record(
                self.name,
                self._runtime.redactor.scrub_text(plan.summary),
                context.user_id if context else None,
                success,
                self._stated_reason(args),
                context_id=context.id if context else None,
            )
        return {
            "done": True,
            **outcome,
            **({"result": result} if result else {}),
            # What logbook and Action history know an action it called by.
            **({"context_id": context.id} if context else {}),
        }
