"""Read general facts about the Home Assistant instance."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.system_info import async_get_system_info
from homeassistant.loader import async_get_integration

# Left out on purpose: the user name and the configuration path.
_SYSTEM_INFO_FIELDS = (
    "version",
    "installation_type",
    "python_version",
    "os_name",
    "os_version",
    "arch",
    "timezone",
)
# Only known on some installations, such as those with a Supervisor.
_OPTIONAL_SYSTEM_INFO_FIELDS = {
    "supervisor": "supervisor_version",
    "host_os": "host_os",
    "docker_version": "docker_version",
    "chassis": "chassis",
    "container_arch": "container_arch",
}
_FRONTEND = "frontend"
_FRONTEND_REQUIREMENT = "home-assistant-frontend=="


async def _async_get_frontend_version(hass: HomeAssistant) -> str | None:
    """Return the version of the frontend, which its manifest states."""
    integration = await async_get_integration(hass, _FRONTEND)
    return next(
        (
            requirement.removeprefix(_FRONTEND_REQUIREMENT)
            for requirement in integration.requirements
            if requirement.startswith(_FRONTEND_REQUIREMENT)
        ),
        None,
    )


async def async_get_system_summary(hass: HomeAssistant) -> dict[str, Any]:
    """Return the versions, platform and state of the instance."""
    info = await async_get_system_info(hass)
    return {
        **{field: info.get(field) for field in _SYSTEM_INFO_FIELDS},
        "frontend_version": await _async_get_frontend_version(hass),
        **{
            name: info[field]
            for field, name in _OPTIONAL_SYSTEM_INFO_FIELDS.items()
            if info.get(field)
        },
        "state": hass.state,
        "language": hass.config.language,
        "recovery_mode": hass.config.recovery_mode,
        "safe_mode": hass.config.safe_mode,
        "loaded_component_count": len(hass.config.components),
    }
