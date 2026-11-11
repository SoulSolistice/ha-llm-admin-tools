"""Withhold sensitive values from everything a tool returns.

Credentials are always withheld. Location, personal and network details are
categories the user can turn on or off, see ``settings.py``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import ipaddress
import re
from typing import Any

from .limits import MAX_SCRUBBED_TEXT_CHARACTERS
from .settings import ExposureSettings

REDACTED = "**REDACTED**"


@dataclass(slots=True, frozen=True)
class _KeyNames:
    """How to recognize a category of sensitive values by their key name."""

    # Whole words, so "author" stays readable while "api_token" does not.
    words: frozenset[str] = frozenset()
    # Spellings that run words together, matched anywhere in the key.
    compounds: tuple[str, ...] = ()
    # Too common as a word of a longer key; sensitive only on their own.
    exact: frozenset[str] = frozenset()


# False positives are accepted: they only hide a value.
_CREDENTIAL_KEYS = _KeyNames(
    words=frozenset(
        {
            "auth",
            "authorization",
            "bearer",
            "cookie",
            "cookies",
            "credential",
            "credentials",
            "passphrase",
            "passwd",
            "password",
            "psk",
            "pwd",
            "secret",
            "secrets",
            "token",
            "tokens",
            "webhook",
        }
    ),
    compounds=(
        "accesskey",
        "accesstoken",
        "apikey",
        "clientsecret",
        "privatekey",
        "refreshtoken",
        "sessionid",
        "tagid",
        "webhookid",
    ),
    exact=frozenset({"code", "pin"}),
)
_LOCATION_KEYS = _KeyNames(
    words=frozenset(
        {
            "altitude",
            "coordinate",
            "coordinates",
            "elevation",
            "gps",
            "lat",
            "latitude",
            "lng",
            "lon",
            "longitude",
        }
    ),
    # As the apps for phones name the coordinates of where a phone is.
    exact=frozenset({"location"}),
)
_PERSONAL_KEYS = _KeyNames(
    words=frozenset({"email", "emails", "username"}),
    compounds=("emailaddress", "phonenumber", "username"),
)
_NETWORK_KEYS = _KeyNames(
    words=frozenset({"bssid", "essid", "ip", "ipv4", "ipv6", "mac", "ssid"}),
    compounds=("hostname",),
    exact=frozenset({"host"}),
)
# Named like a location, but says only how exact one was.
_NEVER_SENSITIVE_KEYS = frozenset({"gps_accuracy"})

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_SEPARATOR = re.compile(r"[^a-z0-9]+")

# Every repeat in these patterns that can restart at each character is bounded. Without
# that, long text that does not match takes quadratic time, and matching holds up all of
# Home Assistant, even in an executor.
_SENSITIVE_NAME = (
    r"[\w-]{0,100}(?:token|secret|password|passwd|pwd|api[_-]?key|auth|sig)[\w-]{0,100}"
)
# A name that holds one of these words, as in access_token, clientSecret or x-api-key.
# What follows a name cannot be part of it, so giving back characters never helps.
_CREDENTIAL_NAME = (
    r"(?:(?:token|secret|passw(?:or)?d|pwd|passphrase|credentials?|psk"
    r"|(?:access|api|auth|private)[_-]?key|(?:session|webhook)[_-]?id)[\w-]{0,100}+"
    # On its own, or "author" and "authenticated" would count.
    r"|(?:(?<![a-z0-9])o?auth|(?-i:Auth))(?:[_-][\w-]{0,100}+)?\b)"
    # What follows InvalidCredentialError: is what went wrong, and no credential.
    r"(?<!error)(?<!exception)"
)
_NAME_ENDS = r"\\?[\"']?\s{0,10}[:=]\s{0,10}"
# In quotes up to where they close, keeping them. An opening quote that does not close
# takes the rest of the line, or what follows it would be left in the clear.
_QUOTED = r"(\")(?:[^\"\\\r\n]|\\.)*\"|(')(?:[^'\\\r\n]|\\.)*'|\\?[\"'][^\r\n]*"
_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # https://user:password@host
    (
        re.compile(
            r"(\b[a-z][a-z0-9+.-]{0,30}://)[^/\s:@]{1,256}:[^/\s@]{1,256}@", re.I
        ),
        rf"\1{REDACTED}@",
    ),
    # ?token=abc&authSig=def
    (re.compile(rf"([?&]{_SENSITIVE_NAME}=)[^&\s\"'#]+", re.I), rf"\1{REDACTED}"),
    # Authorization: Bearer abc
    (re.compile(r"\b(bearer\s+)[\w.~+/=-]+", re.I), rf"\1{REDACTED}"),
    # Basic dXNlcjpwYXNz, but not "Basic authentication", which is a word
    (
        re.compile(
            r"\b((?i:basic)\s{1,10})(?=\S{0,2048}?(?:[0-9+/=]|[a-z][A-Z]))"
            r"[A-Za-z0-9+/]{4,}={0,2}"
        ),
        rf"\1{REDACTED}",
    ),
    # Home Assistant access tokens and other JSON web tokens
    (re.compile(r"\beyJ[\w-]{5,1024}\.[\w-]{5,8192}\.[\w-]*"), REDACTED),
    # Authorization: Digest a="b", c="d" or Cookie: sid=abc; other=def
    (
        re.compile(
            r"((?:authorization|cookies?)" + _NAME_ENDS + r")"
            r"(?:" + _QUOTED + r"|[^\r\n]+)",
            re.I,
        ),
        rf"\1\2\3{REDACTED}\2\3",
    ),
    # password=abc or {'access_token': 'abc'}, as found in log messages
    (
        re.compile(
            rf"({_CREDENTIAL_NAME}{_NAME_ENDS})(?:{_QUOTED}|[^\s\"',;&]+)",
            re.I,
        ),
        rf"\1\2\3{REDACTED}\2\3",
    ),
    # auth_token abcdefghijklmnop: without : or =, only what no sentence has as a word
    (
        re.compile(
            rf"({_CREDENTIAL_NAME}\\?[\"']?\s{{1,10}})[\w.~+/=-]{{16,}}",
            re.I,
        ),
        rf"\1{REDACTED}",
    ),
)
# {'code': '0427'} or {"pin": "0427"}: a dictionary or JSON quoted in a message, which
# says that this is a key, so the rule for keys applies. Without the quotes these can be
# ordinary words, as in "status code: 500", and a number is left as it is for that too.
_QUOTED_FIELD = re.compile(
    rf"""(?<!\w)(\\?["'])([\w-]{{1,100}})(\\?["']\s{{0,10}}[:=]\s{{0,10}})(?:{_QUOTED})"""
)
_EMAIL = re.compile(
    r"\b[\w.+-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){1,8}\b", re.I
)
_MAC_ADDRESS = re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I)
# Candidates only: ipaddress decides, so times like 10:34:13 are left alone. A port or
# the end of a sentence can follow, but not more of what makes it a version number.
_IP_ADDRESS = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\w)"
    r"|(?<![\w.:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![\w:]|\.\w)",
    re.I,
)


def _normalize_key(key: str) -> str:
    """Return a key name in lower case with its words joined by underscores."""
    spaced = _CAMEL_BOUNDARY.sub("_", key).lower()
    return _WORD_SEPARATOR.sub("_", spaced).strip("_")


def _matches(normalized: str, names: _KeyNames) -> bool:
    """Return if a normalized key name belongs to a category."""
    return (
        normalized in names.exact
        or not names.words.isdisjoint(normalized.split("_"))
        or any(name in normalized.replace("_", "") for name in names.compounds)
    )


class Redactor:
    """Remove sensitive values from JSON primitives, whatever the tool read."""

    def __init__(
        self,
        secret_values: Iterable[str] = (),
        settings: ExposureSettings = ExposureSettings(),  # noqa: B008
    ) -> None:
        """Initialize with the exact values that must never leave and the settings."""
        secrets = set(secret_values)
        self.secret_value_count = len(secrets)
        self._fixed_literals = frozenset(secrets | settings.extra_redacted_text)
        self._literals: tuple[str, ...] = ()
        self.set_live_values(())
        self._personal = settings.redact_personal
        self._network = settings.redact_network
        self._key_categories = [_CREDENTIAL_KEYS]
        if settings.redact_location:
            self._key_categories.append(_LOCATION_KEYS)
        if settings.redact_personal:
            self._key_categories.append(_PERSONAL_KEYS)
        if settings.redact_network:
            self._key_categories.append(_NETWORK_KEYS)
        self._extra_keys = frozenset(
            _normalize_key(key) for key in settings.extra_redacted_keys
        )

    def set_live_values(self, values: Iterable[str]) -> None:
        """Replace the values that are withheld for what they are right now.

        Such as the state of a text entity that holds a password, which changes. The
        list is replaced whole, so that text being scrubbed elsewhere sees one or the
        other.
        """
        # Longest first, so a text containing another is removed whole.
        self._literals = tuple(
            sorted(self._fixed_literals | set(values), key=len, reverse=True)
        )

    def is_sensitive_key(self, key: str) -> bool:
        """Return if the value stored under a key name must be withheld."""
        normalized = _normalize_key(key)
        if normalized in self._extra_keys:
            return True
        if normalized in _NEVER_SENSITIVE_KEYS:
            return False
        return any(_matches(normalized, names) for names in self._key_categories)

    def _mask_ip_address(self, match: re.Match[str]) -> str:
        """Return an IP address masked if its category is withheld."""
        text = match.group()
        # As in "2001:db8::1: refused", where the colon belongs to the sentence.
        ends_sentence = text.endswith(":") and not text.endswith("::")
        try:
            address = ipaddress.ip_address(text[:-1] if ends_sentence else text)
        except ValueError:
            return text
        withheld = self._personal if address.is_global else self._network
        if not withheld:
            return text
        return f"{REDACTED}:" if ends_sentence else REDACTED

    def _mask_quoted_field(self, match: re.Match[str]) -> str:
        """Return a quoted field, with its value withheld if its name says so."""
        if not self.is_sensitive_key(match[2]):
            return match.group()
        quote = match[4] or match[5] or ""
        return f"{match[1]}{match[2]}{match[3]}{quote}{REDACTED}{quote}"

    def scrub_text(self, text: str) -> str:
        """Remove known values and sensitive-looking text from a string."""
        if len(text) > MAX_SCRUBBED_TEXT_CHARACTERS:
            return REDACTED
        for literal in self._literals:
            text = text.replace(literal, REDACTED)
        for pattern, replacement in _CREDENTIAL_PATTERNS:
            text = pattern.sub(replacement, text)
        text = _QUOTED_FIELD.sub(self._mask_quoted_field, text)
        if self._personal and "@" in text:
            text = _EMAIL.sub(REDACTED, text)
        if self._network:
            text = _MAC_ADDRESS.sub(REDACTED, text)
        if self._personal or self._network:
            text = _IP_ADDRESS.sub(self._mask_ip_address, text)
        return text

    def redact(self, value: Any) -> Any:
        """Return a redacted copy of a JSON value."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {
                self.scrub_text(key): (
                    REDACTED if self.is_sensitive_key(key) else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value
