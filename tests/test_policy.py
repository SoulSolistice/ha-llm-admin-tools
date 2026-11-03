"""Tests for access, redaction and secret values."""

from pathlib import Path

import pytest

from custom_components.llm_admin_tools.policy.access import (
    async_is_admin,
    async_require_admin,
)
from custom_components.llm_admin_tools.policy.redaction import REDACTED, Redactor
from custom_components.llm_admin_tools.policy.secret_values import (
    SecretFile,
    async_load_secret_files,
)
from custom_components.llm_admin_tools.policy.settings import ExposureSettings
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import llm

from .conftest import make_llm_context


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "apiKey",
        "API-KEY",
        "access_token",
        "accessToken",
        "client_secret",
        "Authorization",
        "webhook_id",
        "private_key",
        "wifi_password",
        "latitude",
        "longitude",
        "gps",
        "altitude",
        "elevation",
        "email",
        "user_name",
        "phoneNumber",
        "code",
        "pin",
    ],
)
def test_sensitive_keys(key: str) -> None:
    """Key names that hold credentials or a location are recognized."""
    assert Redactor().is_sensitive_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "friendly_name",
        "author",
        "authority",
        "code_format",
        "pin_count",
        "state",
        "unit_of_measurement",
        "platform",
        "gps_accuracy",
        "mac",
        "ssid",
        "host",
    ],
)
def test_ordinary_keys(key: str) -> None:
    """Ordinary key names stay readable."""
    assert not Redactor().is_sensitive_key(key)


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("mqtt://user:hunter2@broker:1883", "hunter2"),
        ("/api/camera_proxy/camera.door?token=abc123&x=1", "abc123"),
        ("/api/image?authSig=abc123", "abc123"),
        ("http://cam/video?api_key=abc123", "abc123"),
        ("Authorization: Bearer abc.def-123", "abc.def-123"),
        ("token eyJhbGciOiJIUzI1.eyJpc3MiOiI1.c2lnbmF0dXJl", "eyJhbGciOiJIUzI1"),
        ("login failed password=abc123 for user", "abc123"),
        ('{"api_key": "abc123"}', "abc123"),
        ("the value is exact-secret-value here", "exact-secret-value"),
    ],
)
def test_scrub_text(text: str, leak: str) -> None:
    """Credentials are removed from free text."""
    scrubbed = Redactor({"exact-secret-value"}).scrub_text(text)
    assert leak not in scrubbed
    assert REDACTED in scrubbed


def test_scrub_text_keeps_ordinary_text() -> None:
    """Text without credentials is unchanged."""
    text = "Error updating sensor.power: timeout at http://device/status?id=3"
    assert Redactor().scrub_text(text) == text


def test_scrub_removes_longest_secret_first() -> None:
    """A secret that contains another is removed whole."""
    redactor = Redactor({"secret", "secret-and-more"})
    assert redactor.scrub_text("x secret-and-more y") == f"x {REDACTED} y"
    assert redactor.secret_value_count == 2


def test_redact_nested() -> None:
    """Redaction reaches into nested data, by key and by value."""
    data = {
        "password": {"nested": "gone"},
        "list": [{"token": 1}, "known-value", 2, None],
        "known-value": "key is scrubbed too",
        "number": 3,
    }
    assert Redactor({"known-value"}).redact(data) == {
        "password": REDACTED,
        "list": [{"token": REDACTED}, REDACTED, 2, None],
        REDACTED: "key is scrubbed too",
        "number": 3,
    }


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("account someone.else+tag@example.com logged in", "someone.else"),
        ("login from 8.8.8.8 failed", "8.8.8.8"),
        ("login from 2a00:1450:4001:81b::200e failed", "2a00:1450"),
    ],
)
def test_personal_text_is_withheld_by_default(text: str, leak: str) -> None:
    """Email addresses and public IP addresses are masked."""
    scrubbed = Redactor().scrub_text(text)
    assert leak not in scrubbed
    assert REDACTED in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "device at 192.168.1.20 failed",
        "device fe80::1 failed",
        "device aa:bb:cc:dd:ee:ff failed",
        "update at 10:34:13 from version 1.2.3",
        "out of range: 300.1.1.1",
    ],
)
def test_network_text_stays_by_default(text: str) -> None:
    """Local addresses help diagnosing, and lookalikes are not addresses."""
    assert Redactor().scrub_text(text) == text


@pytest.mark.parametrize("address", ["192.168.1.20", "fe80::1", "aa:bb:cc:dd:ee:ff"])
def test_network_text_withheld_when_chosen(address: str) -> None:
    """Local addresses are masked when the user asks."""
    redactor = Redactor(settings=ExposureSettings(redact_network=True))
    assert redactor.scrub_text(f"device {address} failed") == (
        f"device {REDACTED} failed"
    )


def test_times_survive_network_redaction() -> None:
    """A time looks like an IPv6 address to the pattern, but is not one."""
    redactor = Redactor(settings=ExposureSettings(redact_network=True))
    assert redactor.scrub_text("at 10:34:13") == "at 10:34:13"


@pytest.mark.parametrize("key", ["ip", "mac_address", "ssid", "bssid", "host", "ipv6"])
def test_network_keys_withheld_when_chosen(key: str) -> None:
    """Network keys are a category of their own."""
    redactor = Redactor(settings=ExposureSettings(redact_network=True))
    assert redactor.is_sensitive_key(key)


def test_categories_can_be_turned_off() -> None:
    """Location and personal details are shown if the user prefers."""
    redactor = Redactor(
        settings=ExposureSettings(redact_location=False, redact_personal=False)
    )
    assert not redactor.is_sensitive_key("latitude")
    assert not redactor.is_sensitive_key("email")
    text = "from someone@example.com at 8.8.8.8"
    assert redactor.scrub_text(text) == text


def test_credentials_cannot_be_turned_off() -> None:
    """No setting makes a credential readable."""
    redactor = Redactor(
        settings=ExposureSettings(redact_location=False, redact_personal=False)
    )
    assert redactor.is_sensitive_key("password")
    assert "abc123" not in redactor.scrub_text("password=abc123")


def test_extra_keys_and_text() -> None:
    """The user can add key names, in any spelling, and exact text."""
    redactor = Redactor(
        settings=ExposureSettings(
            extra_redacted_keys=frozenset({"Serial-Number"}),
            extra_redacted_text=frozenset({"my-house-name"}),
        )
    )
    assert redactor.is_sensitive_key("serial_number")
    assert redactor.is_sensitive_key("serialNumber")
    assert redactor.scrub_text("the my-house-name net") == f"the {REDACTED} net"
    # Text the user adds is not counted as a value from a secrets file.
    assert redactor.secret_value_count == 0


async def test_secret_values_loaded(hass: HomeAssistant, tmp_path: Path) -> None:
    """String values long enough to be matched safely are collected."""
    (tmp_path / "secrets.yaml").write_text(
        "logger: warning-is-not-secret\n"
        "wifi: my-wifi-password\n"
        "short: abc\n"
        "port: 1883\n"
        "nested:\n"
        "  - inner-secret-value\n"
        "  - key: mapped-secret-value\n",
        encoding="utf-8",
    )
    hass.config.config_dir = str(tmp_path)
    assert await async_load_secret_files(hass, False) == [
        SecretFile(
            "secrets.yaml",
            True,
            frozenset(
                {"my-wifi-password", "inner-secret-value", "mapped-secret-value"}
            ),
        )
    ]


async def test_esphome_secret_values_loaded(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """ESPHome's own file is read when the setting asks for it."""
    (tmp_path / "esphome").mkdir()
    (tmp_path / "esphome" / "secrets.yaml").write_text(
        "wifi_password: esphome-wifi-password\n", encoding="utf-8"
    )
    hass.config.config_dir = str(tmp_path)
    assert await async_load_secret_files(hass, True) == [
        SecretFile("secrets.yaml", False, frozenset()),
        SecretFile("esphome/secrets.yaml", True, frozenset({"esphome-wifi-password"})),
    ]
    assert await async_load_secret_files(hass, False) == [
        SecretFile("secrets.yaml", False, frozenset()),
    ]


async def test_secret_values_invalid_file(hass: HomeAssistant, tmp_path: Path) -> None:
    """A broken secrets.yaml is an error, not an empty list."""
    (tmp_path / "secrets.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    hass.config.config_dir = str(tmp_path)
    with pytest.raises(HomeAssistantError):
        await async_load_secret_files(hass, False)


async def test_secret_values_unreadable_file(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """An unreadable secrets.yaml is an error, not an empty list."""
    (tmp_path / "secrets.yaml").mkdir()
    hass.config.config_dir = str(tmp_path)
    with pytest.raises(HomeAssistantError):
        await async_load_secret_files(hass, False)


async def test_admin_is_allowed(hass: HomeAssistant, hass_admin_user: User) -> None:
    """An active administrator passes."""
    context = make_llm_context(hass_admin_user.id)
    assert await async_is_admin(hass, context)
    await async_require_admin(hass, context)


async def test_non_admin_is_refused(
    hass: HomeAssistant, hass_read_only_user: User
) -> None:
    """A user who is not an administrator is refused."""
    context = make_llm_context(hass_read_only_user.id)
    assert not await async_is_admin(hass, context)
    with pytest.raises(Unauthorized):
        await async_require_admin(hass, context)


async def test_inactive_admin_is_refused(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    """A deactivated administrator is refused."""
    await hass.auth.async_deactivate_user(hass_admin_user)
    assert not await async_is_admin(hass, make_llm_context(hass_admin_user.id))


@pytest.mark.parametrize("user_id", [None, "unknown-user"])
async def test_unknown_user_is_refused(
    hass: HomeAssistant, user_id: str | None
) -> None:
    """A request without a known user is refused."""
    assert not await async_is_admin(hass, make_llm_context(user_id))


async def test_missing_context_is_refused(hass: HomeAssistant) -> None:
    """A request without any context is refused."""
    context = llm.LLMContext(
        platform="test",
        context=None,
        language="en",
        assistant="conversation",
        device_id=None,
    )
    assert not await async_is_admin(hass, context)
