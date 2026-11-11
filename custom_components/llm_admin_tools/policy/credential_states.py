"""The states that are credentials themselves, which no key name gives away."""

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback

from ..errors import invalid_input
from .exposure import ExposurePolicy
from .limits import MIN_WITHHELD_TEXT_LENGTH

# A text entity or helper in this mode is shown as dots in the UI.
_TEXT_DOMAINS = ("input_text", "text")
_MODE = "mode"
_PASSWORD_MODE = "password"
_NO_VALUE = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


def holds_credential(state: State | None) -> bool:
    """Return if the state of an entity is a password or the like."""
    return (
        state is not None
        and state.domain in _TEXT_DOMAINS
        and state.attributes.get(_MODE) == _PASSWORD_MODE
    )


@callback
def credential_values(hass: HomeAssistant) -> frozenset[str]:
    """Return the states that are credentials right now, to withhold wherever found.

    What is shorter than text the owner can have withheld would be removed from
    everything, leaving nothing readable.
    """
    return frozenset(
        state.state
        for state in hass.states.async_all(_TEXT_DOMAINS)
        if holds_credential(state)
        and state.state not in _NO_VALUE
        and len(state.state) >= MIN_WITHHELD_TEXT_LENGTH
    )


@callback
def require_history_readable(
    hass: HomeAssistant, policy: ExposurePolicy, entity_id: str
) -> None:
    """Raise if the history of an entity is one of credentials it held before.

    Those are no longer its state, so they cannot be recognized in a result. A hidden
    entity is left to get the same answer as one that does not exist.
    """
    if policy.entity_allowed(entity_id) and holds_credential(
        hass.states.get(entity_id)
    ):
        raise invalid_input("history_withheld", entity_id=entity_id)
