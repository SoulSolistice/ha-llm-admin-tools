"""The settings menu: one step for each kind of decision about what is allowed."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import probatio

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    LabelSelector,
    LabelSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALLOWED_SERVICES,
    CONF_ASSIST_EXPOSED_ONLY,
    CONF_ENABLED_TOOLS,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXCLUDED_LABELS,
    CONF_EXCLUDED_LOGGERS,
    CONF_EXTRA_REDACTED_KEYS,
    CONF_EXTRA_REDACTED_TEXT,
    CONF_PROTECTED_AREAS,
    CONF_PROTECTED_DOMAINS,
    CONF_PROTECTED_ENTITIES,
    CONF_PROTECTED_INTEGRATIONS,
    CONF_PROTECTED_LABELS,
    CONF_REDACT_LOCATION,
    CONF_REDACT_NETWORK,
    CONF_REDACT_PERSONAL,
    CONF_SCRUB_ESPHOME_SECRETS,
    CONF_WRITE_ALLOW_DELETE,
    CONF_WRITE_ALLOW_TEMPLATES,
    CONF_WRITE_SUBJECTS,
)
from .policy.limits import MIN_WITHHELD_TEXT_LENGTH
from .policy.settings import (
    DEFAULT_REDACT_LOCATION,
    DEFAULT_REDACT_NETWORK,
    DEFAULT_REDACT_PERSONAL,
    DEFAULT_SCRUB_ESPHOME_SECRETS,
)
from .policy.writes import (
    ALL_SERVICES,
    NEVER_DOMAINS,
    WRITE_SUBJECTS,
    service_entry_possible,
)
from .tools import TOOL_CLASSES
from .tools.catalog import WRITE_TOOL_CLASSES

_ALL_TOOL_NAMES = [tool_class.name for tool_class in TOOL_CLASSES]
_WRITE_TOOL_NAMES = {tool_class.name for tool_class in WRITE_TOOL_CLASSES}
_TEXT_LIST = TextSelector(TextSelectorConfig(multiple=True))
_MAX_ACTIVITY_LINES = 50


def _list_field(key: str) -> probatio.Optional:
    """Return a field that becomes empty when the user clears it.

    Without the default a cleared field would be absent from the input, and the
    value saved before would stay in force.
    """
    return probatio.Optional(key, default=[])


def _plain(text: Any) -> str:
    """Return text from a client on one line, unable to end the block it is shown in."""
    return " ".join(str(text).split()).replace("`", "'")


def _as_text_block(records: Iterable[Mapping[str, Any]]) -> str:
    """Return records as a block of plain text, the newest first.

    It holds text from clients, so it must not be rendered: an image in there would
    make the browser of the owner fetch it.
    """
    lines = []
    for record in list(records)[-_MAX_ACTIVITY_LINES:][::-1]:
        moment = dt_util.as_local(datetime.fromisoformat(record["timestamp"]))
        outcome = "ok" if record["success"] else "failed"
        summary = record.get("summary", record["tool"])
        lines.append(f"{moment:%Y-%m-%d %H:%M} {outcome} {_plain(summary)}")
        if record.get("reason"):
            lines.append(f"    {_plain(record['reason'])}")
    return "```\n" + "\n".join(lines) + "\n```" if lines else "-"


class LlmAdminToolsOptionsFlow(OptionsFlowWithReload):
    """Change what is exposed; saving reloads the entry, which applies it."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the kinds of settings."""
        menu_options = ["tools", "scope", "redaction", "writes"]
        if self.config_entry.state is ConfigEntryState.LOADED:
            menu_options.append("recent_activity")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_recent_activity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what clients did, which stays out of diagnostics; nothing to save."""
        if user_input is not None:
            return await self.async_step_init()
        runtime = self.config_entry.runtime_data
        reads = (
            record
            for record in runtime.audit.as_diagnostics()
            if record.get("reason") and record["tool"] not in _WRITE_TOOL_NAMES
        )
        return self.async_show_form(
            step_id="recent_activity",
            data_schema=probatio.Schema({}),
            description_placeholders={
                "changes": _as_text_block(runtime.journal.recent()),
                "reads": _as_text_block(reads),
            },
        )

    def _async_save(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Save one step's settings, keeping those of the other steps."""
        return self.async_create_entry(data={**self.config_entry.options, **user_input})

    def _async_show_step(
        self,
        step_id: str,
        schema: dict[Any, Any],
        values: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show a form, filled with the given values or else what is saved."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(schema),
                values if values is not None else self.config_entry.options,
            ),
            errors=errors,
        )

    async def async_step_tools(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which tools are offered."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_ENABLED_TOOLS]:
                if set(_ALL_TOOL_NAMES) <= set(user_input[CONF_ENABLED_TOOLS]):
                    # Saved as a list, a tool that a later version adds would start off.
                    return self.async_create_entry(
                        data={
                            key: value
                            for key, value in self.config_entry.options.items()
                            if key != CONF_ENABLED_TOOLS
                        }
                    )
                return self._async_save(user_input)
            errors[CONF_ENABLED_TOOLS] = "no_tools_selected"

        # A tool removed by an update would make the form refuse to save.
        saved = [
            name
            for name in self.config_entry.options.get(
                CONF_ENABLED_TOOLS, _ALL_TOOL_NAMES
            )
            if name in _ALL_TOOL_NAMES
        ]
        schema = {
            _list_field(CONF_ENABLED_TOOLS): SelectSelector(
                SelectSelectorConfig(
                    options=_ALL_TOOL_NAMES,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                    translation_key="tool",
                )
            )
        }
        return self._async_show_step(
            "tools", schema, user_input or {CONF_ENABLED_TOOLS: saved}, errors
        )

    def _integration_options(self, key: str) -> list[SelectOptionDict]:
        """Return the config entries, and any saved one that no longer exists.

        A saved value missing from the options would make the form refuse to save.
        """
        entries = self.hass.config_entries.async_entries()
        known = {entry.entry_id for entry in entries}
        saved = self.config_entry.options.get(key, [])
        return [
            *(
                SelectOptionDict(
                    value=entry.entry_id, label=f"{entry.title} ({entry.domain})"
                )
                for entry in entries
            ),
            *(
                SelectOptionDict(value=entry_id, label=entry_id)
                for entry_id in saved
                if entry_id not in known
            ),
        ]

    async def async_step_scope(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which entities, devices, integrations and logs are visible."""
        if user_input is not None:
            return self._async_save(user_input)

        schema = {
            probatio.Optional(
                CONF_ASSIST_EXPOSED_ONLY, default=False
            ): BooleanSelector(),
            _list_field(CONF_EXCLUDED_DOMAINS): SelectSelector(
                SelectSelectorConfig(
                    options=sorted(
                        {state.domain for state in self.hass.states.async_all()}
                    ),
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            _list_field(CONF_EXCLUDED_ENTITIES): EntitySelector(
                EntitySelectorConfig(multiple=True)
            ),
            _list_field(CONF_EXCLUDED_AREAS): AreaSelector(
                AreaSelectorConfig(multiple=True)
            ),
            _list_field(CONF_EXCLUDED_LABELS): LabelSelector(
                LabelSelectorConfig(multiple=True)
            ),
            _list_field(CONF_EXCLUDED_INTEGRATIONS): SelectSelector(
                SelectSelectorConfig(
                    options=self._integration_options(CONF_EXCLUDED_INTEGRATIONS),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                )
            ),
            _list_field(CONF_EXCLUDED_LOGGERS): _TEXT_LIST,
        }
        return self._async_show_step("scope", schema)

    async def async_step_redaction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which values are withheld, in addition to credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {
                **user_input,
                CONF_EXTRA_REDACTED_TEXT: [
                    text
                    for text in user_input.get(CONF_EXTRA_REDACTED_TEXT, [])
                    if text.strip()
                ],
            }
            if all(
                len(text) >= MIN_WITHHELD_TEXT_LENGTH
                for text in user_input[CONF_EXTRA_REDACTED_TEXT]
            ):
                return self._async_save(user_input)
            errors[CONF_EXTRA_REDACTED_TEXT] = "withheld_text_too_short"

        schema = {
            probatio.Optional(
                CONF_REDACT_LOCATION, default=DEFAULT_REDACT_LOCATION
            ): BooleanSelector(),
            probatio.Optional(
                CONF_REDACT_PERSONAL, default=DEFAULT_REDACT_PERSONAL
            ): BooleanSelector(),
            probatio.Optional(
                CONF_REDACT_NETWORK, default=DEFAULT_REDACT_NETWORK
            ): BooleanSelector(),
            probatio.Optional(
                CONF_SCRUB_ESPHOME_SECRETS, default=DEFAULT_SCRUB_ESPHOME_SECRETS
            ): BooleanSelector(),
            _list_field(CONF_EXTRA_REDACTED_KEYS): _TEXT_LIST,
            _list_field(CONF_EXTRA_REDACTED_TEXT): _TEXT_LIST,
        }
        return self._async_show_step("redaction", schema, user_input, errors)

    async def async_step_writes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what clients can change; nothing can be until the owner chooses."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if all(
                service_entry_possible(entry.strip().lower())
                for entry in user_input[CONF_ALLOWED_SERVICES]
            ):
                return self._async_save(user_input)
            errors[CONF_ALLOWED_SERVICES] = "service_not_possible"

        schema = {
            _list_field(CONF_WRITE_SUBJECTS): SelectSelector(
                SelectSelectorConfig(
                    options=list(WRITE_SUBJECTS),
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                    translation_key="write_subject",
                )
            ),
            probatio.Optional(
                CONF_WRITE_ALLOW_DELETE, default=False
            ): BooleanSelector(),
            probatio.Optional(
                CONF_WRITE_ALLOW_TEMPLATES, default=False
            ): BooleanSelector(),
            _list_field(CONF_ALLOWED_SERVICES): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        ALL_SERVICES,
                        *sorted(
                            set(self.hass.services.async_services()) - NEVER_DOMAINS
                        ),
                    ],
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            _list_field(CONF_PROTECTED_DOMAINS): SelectSelector(
                SelectSelectorConfig(
                    options=sorted(
                        {state.domain for state in self.hass.states.async_all()}
                    ),
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            _list_field(CONF_PROTECTED_ENTITIES): EntitySelector(
                EntitySelectorConfig(multiple=True)
            ),
            _list_field(CONF_PROTECTED_AREAS): AreaSelector(
                AreaSelectorConfig(multiple=True)
            ),
            _list_field(CONF_PROTECTED_LABELS): LabelSelector(
                LabelSelectorConfig(multiple=True)
            ),
            _list_field(CONF_PROTECTED_INTEGRATIONS): SelectSelector(
                SelectSelectorConfig(
                    options=self._integration_options(CONF_PROTECTED_INTEGRATIONS),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                )
            ),
        }
        return self._async_show_step("writes", schema, user_input, errors)
