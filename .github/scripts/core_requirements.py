"""Print what the integrations the tests run against ask for in this Home Assistant."""

import json
from pathlib import Path

from homeassistant import components

_INTEGRATIONS = ("conversation", "mcp_server")

for integration in _INTEGRATIONS:
    manifest = Path(components.__path__[0]) / integration / "manifest.json"
    print("\n".join(json.loads(manifest.read_text())["requirements"]))
