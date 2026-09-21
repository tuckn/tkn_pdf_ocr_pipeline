from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import OcrError


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise OcrError(f"Expected a file: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path: Path, data: bytes, *, replace: bool = True) -> None:
    """Stage on the destination filesystem; publish complete bytes only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    stage = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(stage, path)
        else:
            os.link(stage, path)  # Atomic creation; cannot clobber an existing file.
    finally:
        stage.unlink(missing_ok=True)


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "1.0.0":
            raise ValueError("unsupported state")
        return value
    except (ValueError, OSError) as exc:
        raise OcrError(f"Invalid state file; preserve it and investigate: {path}") from exc


def backup(path: Path, expected: str) -> Path:
    data = path.read_bytes()
    if sha256(data) != expected:
        raise OcrError(f"File changed before backup: {path}")
    target = path.with_name(f"{path.name}.bak-{uuid4().hex}")
    atomic_write(target, data, replace=False)
    return target
