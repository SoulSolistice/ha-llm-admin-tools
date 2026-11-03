"""The LLM Admin Tools integration: admin tools for LLM clients."""

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from .actions.definitions import DefinitionEditor
from .audit import AuditLog
from .const import DOMAIN
from .journal import WriteJournal
from .llm_api import async_register_llm_api
from .models import LlmAdminToolsConfigEntry, RuntimeData
from .policy.exposure import ExposurePolicy
from .policy.redaction import Redactor
from .policy.secret_values import async_load_secret_files
from .policy.settings import ExposureSettings, WriteSettings
from .policy.writes import WritePolicy


async def async_setup_entry(
    hass: HomeAssistant, entry: LlmAdminToolsConfigEntry
) -> bool:
    """Set up LLM Admin Tools from a config entry."""
    settings = ExposureSettings.from_options(entry.options)
    try:
        secret_files = await async_load_secret_files(
            hass, settings.scrub_esphome_secrets
        )
    except HomeAssistantError as err:
        # Fail closed: without the secret values, results could leak them.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="secrets_unreadable"
        ) from err

    secret_values = {value for file in secret_files for value in file.values}
    exposure = ExposurePolicy(hass, settings)
    journal = WriteJournal(hass)
    await journal.async_load()
    audit = AuditLog(hass)
    await audit.async_load()
    entry.runtime_data = RuntimeData(
        audit=audit,
        exposure=exposure,
        redactor=Redactor(secret_values, settings),
        secret_files=secret_files,
        writes=WritePolicy(hass, exposure, WriteSettings.from_options(entry.options)),
        journal=journal,
        definitions=DefinitionEditor(hass),
    )
    entry.async_on_unload(async_register_llm_api(hass, entry.runtime_data))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: LlmAdminToolsConfigEntry
) -> bool:
    """Unload a config entry; the API is unregistered by its unload callback."""
    await entry.runtime_data.journal.async_save()
    await entry.runtime_data.audit.async_save()
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: LlmAdminToolsConfigEntry
) -> None:
    """Remove what is stored when the integration is removed."""
    await WriteJournal(hass).async_remove()
    await AuditLog(hass).async_remove()
