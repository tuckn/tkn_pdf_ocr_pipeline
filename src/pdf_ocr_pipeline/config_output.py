"""Render resolved configuration as copyable, single-line entries."""

from __future__ import annotations

import json
from typing import Any


def _display_string(value: str) -> str:
    escapes = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return "".join(
        escapes.get(char, f"\\u{ord(char):04x}")
        if ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029"
        else char
        for char in value
    )


def config_lines(value: Any, prefix: str = "") -> list[str]:
    """Flatten mappings and lists while keeping strings and Windows paths copyable."""
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}={{}}"]
        return [
            line
            for key, item in value.items()
            for line in config_lines(item, f"{prefix}.{key}" if prefix else key)
        ]
    if isinstance(value, list):
        if not value:
            return [f"{prefix}=[]"]
        return [
            line
            for index, item in enumerate(value)
            for line in config_lines(item, f"{prefix}[{index}]")
        ]
    display = _display_string(value) if isinstance(value, str) else json.dumps(value)
    return [f"{prefix}={display}"]
