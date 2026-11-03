"""The values of secrets files, so results can be scrubbed of them."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import SECRET_YAML, load_yaml_dict

ESPHOME_SECRET_YAML = f"esphome/{SECRET_YAML}"

# Shorter strings match too much unrelated text. Numbers are never scrubbed.
_MIN_SECRET_LENGTH = 6
# Not a secret: Home Assistant reads this key to set up its own secret logging.
_IGNORED_KEYS = frozenset({"logger"})


@dataclass(slots=True, frozen=True)
class SecretFile:
    """What was found in one secrets file."""

    name: str
    found: bool
    values: frozenset[str]


def _string_values(value: Any) -> Iterator[str]:
    """Yield the strings held by a YAML value."""
    if isinstance(value, str):
        yield str(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)


def _load_secret_file(config_dir: Path, name: str) -> SecretFile:
    """Read a file from disk; a missing one holds no values."""
    path = config_dir / name
    if not path.exists():
        return SecretFile(name, found=False, values=frozenset())
    # Raises HomeAssistantError if the file cannot be read or is not a mapping.
    secrets = load_yaml_dict(str(path))
    return SecretFile(
        name,
        found=True,
        values=frozenset(
            value
            for key, item in secrets.items()
            if key not in _IGNORED_KEYS
            for value in _string_values(item)
            if len(value) >= _MIN_SECRET_LENGTH
        ),
    )


async def async_load_secret_files(
    hass: HomeAssistant, include_esphome: bool
) -> list[SecretFile]:
    """Return what the secrets files of the configuration directory hold."""
    names = [SECRET_YAML, ESPHOME_SECRET_YAML] if include_esphome else [SECRET_YAML]
    config_dir = Path(hass.config.config_dir)
    return [
        await hass.async_add_executor_job(_load_secret_file, config_dir, name)
        for name in names
    ]
