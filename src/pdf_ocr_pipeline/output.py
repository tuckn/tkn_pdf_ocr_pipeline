"""CLI presentation only; Python results and persisted run reports stay intact."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .config_output import config_lines


def public_result(result: dict[str, Any], *, full_output: bool = False) -> dict[str, Any]:
    """Summarize bulk skips identically in text and JSON, retaining every attention target."""
    public = dict(result)
    if "counts" in result:
        counts = result["counts"]
        public.setdefault(
            "status",
            "failed" if counts.get("failed", 0) or counts.get("needs_review", 0) else "success",
        )
    else:
        public.setdefault("status", "success")
    items = result.get("files", [])
    if not full_output and len(items) > 1:
        skipped = [item for item in items if item.get("status") == "skipped"]
        if skipped:
            public["files"] = [item for item in items if item.get("status") != "skipped"]
            public["omitted_files"] = len(skipped)
            public["skipped_reasons"] = dict(
                Counter(item.get("reason", "unspecified") for item in skipped)
            )
            public["details_hint"] = (
                "Set --full-output when invoking the command to include every file. "
                "For completed runs, run_report contains the full results."
            )
    return public


def emit(result: dict[str, Any], *, json_output: bool) -> None:
    """Emit a single JSON document or copyable entries, independent of TTY/log verbosity."""
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("\n".join(config_lines(result)))
