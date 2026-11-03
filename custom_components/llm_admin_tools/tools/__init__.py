"""The LLM tools: what a client can call and how, one module per subject."""

from homeassistant.helpers import llm

from ..models import RuntimeData
from .base import BaseTool
from .capabilities import GetCapabilitiesTool
from .catalog import READ_TOOL_CLASSES, TOOL_CLASSES_FOR_CHANGES

TOOL_CLASSES: tuple[type[BaseTool], ...] = (GetCapabilitiesTool, *READ_TOOL_CLASSES)


def build_tools(runtime: RuntimeData) -> list[llm.Tool]:
    """Return one instance of every tool the settings enable.

    A tool left out here cannot be called either: Home Assistant only dispatches
    to the tools of the API instance.
    """
    return [
        *(
            tool_class(runtime)
            for tool_class in TOOL_CLASSES
            if runtime.exposure.tool_enabled(tool_class.name)
        ),
        *(
            tool_class(runtime)
            for tool_class in TOOL_CLASSES_FOR_CHANGES
            if any(map(runtime.writes.subject_enabled, tool_class.subjects))
        ),
    ]
