"""Constants for the LLM Admin Tools integration."""

import logging

DOMAIN = "llm_admin_tools"
LOGGER = logging.getLogger(__package__)

# Core calls for a hyphen in the path of a dashboard, so none can have this one.
DEFAULT_DASHBOARD = "default"

LLM_API_NAME = "LLM Admin Tools"

CONF_ENABLED_TOOLS = "enabled_tools"

CONF_ASSIST_EXPOSED_ONLY = "assist_exposed_only"
CONF_EXCLUDED_AREAS = "excluded_areas"
CONF_EXCLUDED_DOMAINS = "excluded_domains"
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_EXCLUDED_INTEGRATIONS = "excluded_integrations"
CONF_EXCLUDED_LABELS = "excluded_labels"
CONF_EXCLUDED_LOGGERS = "excluded_loggers"

CONF_EXTRA_REDACTED_KEYS = "extra_redacted_keys"
CONF_EXTRA_REDACTED_TEXT = "extra_redacted_text"
CONF_REDACT_LOCATION = "redact_location"
CONF_REDACT_NETWORK = "redact_network"
CONF_REDACT_PERSONAL = "redact_personal"
CONF_SCRUB_ESPHOME_SECRETS = "scrub_esphome_secrets"

CONF_ALLOWED_SERVICES = "allowed_services"
CONF_PROTECTED_AREAS = "protected_areas"
CONF_PROTECTED_DOMAINS = "protected_domains"
CONF_PROTECTED_ENTITIES = "protected_entities"
CONF_PROTECTED_INTEGRATIONS = "protected_integrations"
CONF_PROTECTED_LABELS = "protected_labels"
CONF_WRITE_ALLOW_CUSTOM_CARDS = "write_allow_custom_cards"
CONF_WRITE_ALLOW_DELETE = "write_allow_delete"
CONF_WRITE_ALLOW_TEMPLATES = "write_allow_templates"
CONF_WRITE_SUBJECTS = "write_subjects"
