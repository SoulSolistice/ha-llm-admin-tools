"""What the settings let a client change, on top of what it may see."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)

from ..errors import invalid_input
from .exposure import ExposurePolicy
from .members import Members, config_entry_members, members_of
from .settings import WriteSettings

WRITE_SUBJECTS = (
    "services",
    "automations",
    "scripts",
    "scenes",
    "entities",
    "devices",
    "areas",
    "floors",
    "labels",
    "categories",
    "helpers",
    "persons",
    "dashboards",
    "config_entries",
)

# Whatever the settings say: these run arbitrary code, stop the system, or would let a
# client change its own access.
NEVER_DOMAINS = frozenset(
    {
        "command_line",
        "llm_admin_tools",
        "mcp",
        "mcp_server",
        "pyscript",
        "python_script",
        "shell_command",
    }
)
NEVER_SERVICES = frozenset(
    {
        "hassio.addon_stdin",
        "hassio.host_reboot",
        "hassio.host_shutdown",
        "homeassistant.stop",
    }
)
# These hold too much of everything to allow at once, so each service is named.
NAMED_SERVICES_ONLY_DOMAINS = frozenset({"hassio", "homeassistant"})
# A client must not be able to change, or get rid of, what limits it.
NEVER_CONFIG_ENTRY_DOMAINS = frozenset({"llm_admin_tools", "mcp_server"})
# Each script also has a service of its own; these are the ones of the domain.
_SCRIPT_DOMAIN_SERVICES = frozenset({"reload", "toggle", "turn_off", "turn_on"})
NEVER_TOUCHED = (
    "users, authentication and tokens",
    "secrets.yaml and other files",
    "dashboard resources, as they load code",
    "the settings of this integration and of the MCP server",
)


def _names_in_state(state: State | None) -> list[str]:
    """Return the entities that a state says it stands for."""
    members = state.attributes.get(ATTR_ENTITY_ID) if state is not None else None
    if isinstance(members, str):
        return [members]
    if isinstance(members, list | tuple | set | frozenset):
        return [member for member in members if isinstance(member, str)]
    return []


def service_entry_possible(entry: str) -> bool:
    """Return if an entry of the allowed services can ever take effect."""
    domain, _, service = entry.partition(".")
    if domain in NEVER_DOMAINS or entry in NEVER_SERVICES:
        return False
    return bool(service) or domain not in NAMED_SERVICES_ONLY_DOMAINS


class WritePolicy:
    """Answer what may be changed, raising with what a client may know of why.

    Something hidden looks the same as something that does not exist. Something
    protected is visible, so a client is told that it cannot be changed.
    """

    def __init__(
        self, hass: HomeAssistant, exposure: ExposurePolicy, settings: WriteSettings
    ) -> None:
        """Initialize the policy."""
        self.hass = hass
        self._exposure = exposure
        self._protection = ExposurePolicy(hass, settings.protection_as_scope())
        self.settings = settings

    @property
    def reading_restricted(self) -> bool:
        """Return if some entities are kept from clients."""
        settings = self._exposure.settings
        return settings.assist_exposed_only or any(
            (
                settings.excluded_areas,
                settings.excluded_domains,
                settings.excluded_entities,
                settings.excluded_integrations,
                settings.excluded_labels,
            )
        )

    @property
    def restricted(self) -> bool:
        """Return if some entities are hidden or protected."""
        settings = self.settings
        return self.reading_restricted or any(
            (
                settings.protected_areas,
                settings.protected_domains,
                settings.protected_entities,
                settings.protected_integrations,
                settings.protected_labels,
            )
        )

    @property
    def templates_allowed(self) -> bool:
        """Return if what a client writes may hold templates."""
        return self.settings.allow_templates and not self.reading_restricted

    def require_templates(self) -> None:
        """Raise unless what a client writes may hold templates.

        A template can read anything, whatever is hidden or withheld from the client,
        and a trace or a state then gives what it read back.
        """
        if self.reading_restricted:
            raise invalid_input("template_refused")
        if not self.settings.allow_templates:
            raise invalid_input("templates_off")

    def subject_enabled(self, subject: str) -> bool:
        """Return if the owner lets clients change this kind of thing."""
        return subject in self.settings.subjects

    def require_subject(self, subject: str) -> None:
        """Raise unless this kind of thing can be changed."""
        if not self.subject_enabled(subject):
            raise invalid_input("write_subject_off", subject=subject)

    def require_delete(self) -> None:
        """Raise unless the owner lets clients delete things."""
        if not self.settings.allow_delete:
            raise invalid_input("delete_not_allowed")

    def service_allowed(self, domain: str, service: str) -> bool:
        """Return if a service may be called, or be called later by a definition."""
        name = f"{domain}.{service}"
        allowed = self.settings.allowed_services
        if not service_entry_possible(name):
            return False
        return name in allowed or (
            domain in allowed and domain not in NAMED_SERVICES_ONLY_DOMAINS
        )

    def require_service(self, domain: str, service: str) -> None:
        """Raise unless a service may be called."""
        if not self.service_allowed(domain, service):
            raise invalid_input("service_not_allowed", service=f"{domain}.{service}")

    def _script_of_service(self, domain: str, service: str) -> str | None:
        """Return the entity of the script a service runs, if it runs one.

        The service is named after the unique ID, which stays if the entity is renamed.
        """
        if domain != "script" or service in _SCRIPT_DOMAIN_SERVICES:
            return None
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id("script", "script", service) or (
            f"script.{service}"
        )

    @callback
    def service_visible(self, domain: str, service: str) -> bool:
        """Return if a service may be known of: not if it runs a hidden script."""
        script = self._script_of_service(domain, service)
        return script is None or self._exposure.entity_allowed(script)

    @callback
    def require_service_target(self, domain: str, service: str) -> None:
        """Raise unless the script a service runs may be run, naming no other name."""
        script = self._script_of_service(domain, service)
        if script is None:
            return
        if not self._exposure.entity_allowed(script):
            raise invalid_input("entity_not_found", entity_id=f"{domain}.{service}")
        self.require_entity(script)

    def _entity_exists(self, entity_id: str) -> bool:
        """Return if an entity has a state or a registry entry."""
        return (
            self.hass.states.get(entity_id) is not None
            or er.async_get(self.hass).async_get(entity_id) is not None
        )

    @callback
    def require_visible_entity(self, entity_id: str) -> None:
        """Raise unless an entity may be seen.

        Once anything is hidden, a name that does not exist is refused as well: if
        only what is hidden were refused, the refusal would tell that it is there.
        """
        if not self._exposure.entity_allowed(entity_id) or (
            self.reading_restricted and not self._entity_exists(entity_id)
        ):
            raise invalid_input("entity_not_found", entity_id=entity_id)

    @callback
    def require_entity(self, entity_id: str) -> None:
        """Raise unless an entity may be changed."""
        self.require_visible_entity(entity_id)
        if not self._protection.entity_allowed(entity_id):
            raise invalid_input("entity_protected", entity_id=entity_id)

    @callback
    def require_target(self, entity_id: str) -> None:
        """Raise unless an entity may be acted on, and all that it stands for.

        A group passes a service call on to its members, as do light groups and the
        like, and a scene sets them. They are listed in the state, at any depth.
        """
        self.require_entity(entity_id)
        seen = {entity_id}
        waiting = [entity_id]
        while waiting:
            state = self.hass.states.get(waiting.pop())
            for member in _names_in_state(state):
                if member in seen:
                    continue
                if not (
                    self._exposure.entity_allowed(member)
                    and self._protection.entity_allowed(member)
                ):
                    raise invalid_input("target_holds_protected", entity_id=entity_id)
                seen.add(member)
                waiting.append(member)

    @callback
    def require_existing_entity(self, entity_id: str) -> None:
        """Raise unless an entity exists and may be changed, for acting on it now."""
        if not self._entity_exists(entity_id):
            raise invalid_input("entity_not_found", entity_id=entity_id)
        self.require_entity(entity_id)

    def require_domain(self, domain: str) -> None:
        """Raise unless things of a domain may be created or changed.

        This covers what does not exist yet, or has no entity to check.
        """
        if domain in self._exposure.settings.excluded_domains:
            raise invalid_input("write_subject_off", subject=domain)
        if domain in self.settings.protected_domains:
            raise invalid_input("domain_protected", domain=domain)

    def require_visible_device(self, device_id: str) -> dr.AnyDeviceEntry:
        """Return a device, raising unless it exists and may be seen."""
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or not self._exposure.device_allowed(device):
            raise invalid_input("device_not_found", device_id=device_id)
        return device

    def require_device(self, device_id: str) -> dr.AnyDeviceEntry:
        """Return a device, raising unless it exists and may be changed."""
        device = self.require_visible_device(device_id)
        if not self._protection.device_allowed(device):
            raise invalid_input("device_protected", device_id=device_id)
        return device

    def require_area(self, area_id: str) -> None:
        """Raise unless an area may be changed or have things moved into it."""
        if not self._exposure.area_allowed(area_id):
            raise invalid_input("area_not_found", area_id=area_id)
        if not self._protection.area_allowed(area_id):
            raise invalid_input("area_protected", area_id=area_id)
        if ar.async_get(self.hass).async_get_area(area_id) is None:
            raise invalid_input("area_not_found", area_id=area_id)

    def require_floor(self, floor_id: str) -> None:
        """Raise unless a floor may be changed; that also changes its areas."""
        for area in ar.async_get(self.hass).async_list_areas():
            if area.floor_id == floor_id:
                # Naming the area would tell a client what it may not know of.
                if not self._exposure.area_allowed(area.id):
                    raise invalid_input("in_use_by_protected")
                self.require_area(area.id)

    def require_label(self, label_id: str) -> None:
        """Raise unless a label may be changed or given to something."""
        if label_id in self._exposure.settings.excluded_labels:
            raise invalid_input("label_not_found", label_id=label_id)
        if label_id in self.settings.protected_labels:
            raise invalid_input("label_protected", label_id=label_id)
        if lr.async_get(self.hass).async_get_label(label_id) is None:
            raise invalid_input("label_not_found", label_id=label_id)

    def _require_writable(self, members: Members) -> None:
        """Raise if changing what these belong to would change a protected one."""
        writable = all(
            self._exposure.entity_allowed(entity_id)
            and self._protection.entity_allowed(entity_id)
            for entity_id in members.entity_ids
        ) and all(
            self._exposure.device_allowed(device)
            and self._protection.device_allowed(device)
            for device in members.devices
        )
        if not writable:
            raise invalid_input("in_use_by_protected")

    def require_area_deletable(self, area_id: str) -> None:
        """Raise unless deleting an area leaves what is protected alone."""
        self._require_writable(
            members_of(
                self.hass,
                er.async_entries_for_area(er.async_get(self.hass), area_id),
                dr.async_entries_for_area(dr.async_get(self.hass), area_id),
            )
        )

    def require_label_deletable(self, label_id: str) -> None:
        """Raise unless deleting a label leaves what is protected alone."""
        self._require_writable(
            members_of(
                self.hass,
                er.async_entries_for_label(er.async_get(self.hass), label_id),
                dr.async_entries_for_label(dr.async_get(self.hass), label_id),
            )
        )

    def require_device_members_writable(self, device: dr.AnyDeviceEntry) -> None:
        """Raise unless changing a device leaves what is protected alone.

        Its entities and child devices get their name and area from it, and are disabled
        or removed with it, so protecting one of them holds for the device too.
        """
        self._require_writable(members_of(self.hass, [], [device]))

    def require_config_entry_members_writable(self, entry_id: str) -> None:
        """Raise unless what starts or stops with a config entry may be changed."""
        self._require_writable(config_entry_members(self.hass, entry_id))

    def require_config_entry(self, entry_id: str) -> ConfigEntry:
        """Return a config entry, raising unless it exists and may be changed."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or not self._exposure.integration_allowed(entry_id):
            raise invalid_input("config_entry_not_found", entry_id=entry_id)
        if (
            entry.domain in NEVER_CONFIG_ENTRY_DOMAINS
            or not self._protection.integration_allowed(entry_id)
        ):
            raise invalid_input("config_entry_protected", entry_id=entry_id)
        return entry
