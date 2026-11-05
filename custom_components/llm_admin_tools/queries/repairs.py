"""Read the repairs Home Assistant lists, with what Spook found if it is there."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    entity_registry as er,
    issue_registry as ir,
    translation,
)

from ..policy.exposure import ExposurePolicy
from .compact import without_empty_fields
from .mentions import mentions

# Spook only adds to what is here, and nothing depends on it. It reports what it finds
# as repairs, so there is nothing else to talk to, and those are recognized by these
# names.
SPOOK_DOMAIN = "spook"
_SPOOK_UNKNOWN_REFERENCES = "unknown"
_LANGUAGE = "en"
_SEVERITY_ORDER = ("critical", "error", "warning")


@dataclass(slots=True, frozen=True, kw_only=True)
class RepairQuery:
    """Which repairs to return."""

    domain: str | None = None
    include_ignored: bool = False


@callback
def spook_active(hass: HomeAssistant) -> bool:
    """Return if Spook is there to look for what is broken."""
    return SPOOK_DOMAIN in hass.config.components


def _names_something_hidden(
    hass: HomeAssistant, policy: ExposurePolicy, issue: ir.IssueEntry
) -> bool:
    """Return if the text of a repair would show that something hidden is there."""
    registry = er.async_get(hass)
    return any(
        (hass.states.get(name) is not None or registry.async_is_registered(name))
        and not policy.entity_allowed(name)
        for name in mentions(issue.translation_placeholders or {})
        if "." in name
    )


@callback
def visible_issues(
    hass: HomeAssistant, policy: ExposurePolicy, query: RepairQuery
) -> list[ir.IssueEntry]:
    """Return the repairs that may be shown, the most severe and newest first."""
    hidden_domains = {
        entry.domain
        for entry in hass.config_entries.async_entries()
        if not policy.integration_allowed(entry.entry_id)
    }
    issues = [
        issue
        for issue in ir.async_get(hass).issues.values()
        if issue.active
        and (query.include_ignored or issue.dismissed_version is None)
        and (query.domain is None or query.domain == issue.domain)
        and not hidden_domains.intersection((issue.domain, issue.issue_domain))
        # From 2026.11 a repair can belong to an entry of another integration.
        and policy.integration_allowed(getattr(issue, "config_entry_id", None))
        and not _names_something_hidden(hass, policy, issue)
    ]
    newest_first = sorted(issues, key=lambda issue: issue.created, reverse=True)
    return sorted(newest_first, key=lambda issue: _severity_rank(issue.severity))


def _severity_rank(severity: ir.IssueSeverity | None) -> int:
    """Return how an issue sorts by severity; one without any comes last."""
    if severity in _SEVERITY_ORDER:
        return _SEVERITY_ORDER.index(severity)
    return len(_SEVERITY_ORDER)


def _text(issue: ir.IssueEntry, text: str | None) -> str | None:
    """Return a text of a repair with its placeholders filled in."""
    if text is None:
        return None
    for name, value in (issue.translation_placeholders or {}).items():
        text = text.replace(f"{{{name}}}", value)
    return text


async def async_describe_issues(
    hass: HomeAssistant, issues: list[ir.IssueEntry], *, descriptions: bool
) -> list[dict[str, Any]]:
    """Return what a client needs of repairs, leaving out what is not set."""
    texts = await translation.async_get_translations(
        hass, _LANGUAGE, "issues", {issue.domain for issue in issues}
    )
    described = []
    for issue in issues:
        key = f"component.{issue.domain}.issues.{issue.translation_key}"
        description = texts.get(f"{key}.description") or texts.get(
            f"{key}.fix_flow.step.confirm.description"
        )
        described.append(
            without_empty_fields(
                {
                    "issue_id": issue.issue_id,
                    "domain": issue.domain,
                    "about": issue.issue_domain,
                    "kind": issue.translation_key,
                    "severity": issue.severity,
                    "title": _text(issue, texts.get(f"{key}.title")),
                    "description": _text(issue, description) if descriptions else None,
                    "details": issue.translation_placeholders or None,
                    "can_be_fixed": issue.is_fixable or None,
                    "breaks_in_version": issue.breaks_in_ha_version,
                    "learn_more": issue.learn_more_url,
                    "created": issue.created,
                    "ignored": True if issue.dismissed_version else None,
                }
            )
        )
    return described


@callback
def repairs_naming(
    hass: HomeAssistant,
    policy: ExposurePolicy,
    names: set[str],
    redact: Callable[[Any], Any],
) -> list[dict[str, Any]]:
    """Return which repairs name one of these, in lower case.

    These can be what a client typed, which could be a guess at a withheld value.
    """
    return [
        without_empty_fields(
            {
                "issue_id": issue.issue_id,
                "domain": issue.domain,
                "kind": issue.translation_key,
            }
        )
        for issue in visible_issues(hass, policy, RepairQuery())
        if names & mentions(redact(dict(issue.translation_placeholders or {})))
    ]


@callback
def spook_unknown_references(
    hass: HomeAssistant, policy: ExposurePolicy
) -> list[dict[str, Any]]:
    """Return what Spook found to name something unknown.

    It also looks where this integration cannot, such as in template entities and
    groups, and suggests what may have been meant.
    """
    return [
        {
            "kind": issue.translation_key,
            "details": issue.translation_placeholders,
        }
        for issue in visible_issues(hass, policy, RepairQuery(domain=SPOOK_DOMAIN))
        if _SPOOK_UNKNOWN_REFERENCES in (issue.translation_key or "")
    ]
