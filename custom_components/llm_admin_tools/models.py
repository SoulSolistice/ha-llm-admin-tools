"""Typed runtime data of the LLM Admin Tools integration."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .actions.definitions import DefinitionEditor
from .audit import AuditLog
from .journal import WriteJournal
from .policy.exposure import ExposurePolicy
from .policy.redaction import Redactor
from .policy.secret_values import SecretFile
from .policy.writes import WritePolicy


@dataclass(slots=True, frozen=True)
class RuntimeData:
    """Objects that live as long as the config entry is loaded."""

    audit: AuditLog
    exposure: ExposurePolicy
    redactor: Redactor
    secret_files: list[SecretFile]
    writes: WritePolicy
    journal: WriteJournal
    definitions: DefinitionEditor


type LlmAdminToolsConfigEntry = ConfigEntry[RuntimeData]
