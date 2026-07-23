"""Build Ollama tool JSON schemas from Python functions.

STUDY GUIDE
-----------
* Introspects function signatures and docstrings to produce LLM tool definitions.
* Maps Python type hints to JSON Schema types (string, integer, array, etc.).
* Key concepts: ``inspect.signature``, ``get_type_hints``, regex doc parsing, list comprehensions.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, get_type_hints

# LEARN: Dict maps Python types to JSON Schema type names for the tool API.
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _json_type(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "string"
    # LEARN: __origin__ on typing annotations reveals list, dict, Optional, etc.
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        return "array"
    return _TYPE_MAP.get(annotation, "string")


def _parse_param_descriptions(doc: str) -> dict[str, str]:
    """Extract Args: section lines like ``query: The search term``."""
    if not doc:
        return {}
    match = re.search(r"Args:\s*\n(.*?)(?:\n\s*\n|\n\s*Returns:|\Z)", doc, re.DOTALL)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, desc = line.partition(":")
        out[name.strip()] = desc.strip()
    return out


def function_to_tool(fn) -> dict[str, Any]:
    # LEARN: inspect.signature reads parameter names, defaults, and annotations from source.
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    doc = inspect.getdoc(fn) or ""
    param_docs = _parse_param_descriptions(doc)
    summary = doc.split("\n\n")[0].strip() if doc else fn.__name__

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        ann = hints.get(name, param.annotation)
        properties[name] = {
            "type": _json_type(ann),
            "description": param_docs.get(name, name),
        }
        # LEARN: Parameters without defaults are required in JSON Schema "required" array.
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": summary,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def build_tools(functions: list) -> list[dict[str, Any]]:
    # LEARN: List comprehension — compact way to transform every function in the list.
    return [function_to_tool(fn) for fn in functions]
