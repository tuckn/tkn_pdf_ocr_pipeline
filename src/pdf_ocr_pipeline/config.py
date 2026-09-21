from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .errors import OcrError
from .io_utils import atomic_write, backup, file_hash

APPLICATION_ID = "pdf_ocr_pipeline"
SCHEMA_VERSION = "2.0.0"
VERSION_PATTERN = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def example_bytes() -> bytes:
    return files("pdf_ocr_pipeline").joinpath("resources/config.example.yaml").read_bytes()


@dataclass(frozen=True)
class AzureConfig:
    endpoint: str | None
    auth_mode: str
    key_env: str
    api_version: str
    locale: str | None
    request_timeout_seconds: int
    poll_interval_seconds: int
    poll_timeout_seconds: int
    max_get_retries: int
    tenant_id: str | None = None


@dataclass(frozen=True)
class Config:
    input_dir: Path | None
    output_dir: Path | None
    state_dir: Path
    recursive: bool
    min_age_seconds: int
    max_pages: int
    max_file_mb: int
    azure: AzureConfig


@dataclass(frozen=True)
class ResolvedConfig:
    config: Config
    values: dict[str, Any]
    sources: list[dict[str, Any]]
    winning_sources: dict[str, str]


def user_config_path() -> Path:
    return Path.home() / ".tkn" / APPLICATION_ID / "config.yaml"


def _validate(mapping: dict[str, Any], template: dict[str, Any], source: str) -> None:
    for key, value in mapping.items():
        if key in {"existing_text", "redo_dpi"}:
            raise OcrError(
                f"{source}.{key} was removed in 0.3.0; remove this setting. "
                "Pages are classified automatically; use --redo-ocr to replace invisible OCR text."
            )
        if key not in template:
            raise OcrError(f"Unknown config key {source}.{key}")
        expected = template[key]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise OcrError(f"{source}.{key} must be a mapping")
            _validate(value, expected, f"{source}.{key}")
        elif expected is None:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise OcrError(f"{source}.{key} must be a nonempty string or null")
        elif type(value) is not type(expected):
            raise OcrError(f"Wrong type for {source}.{key}; expected {type(expected).__name__}")
        elif isinstance(value, str) and not value.strip():
            raise OcrError(f"{source}.{key} cannot be empty")
        elif type(value) is int and value < 0:
            raise OcrError(f"{source}.{key} cannot be negative")


def _version(value: Any, source: str) -> None:
    match = VERSION_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise OcrError(f'{source}: schema_version is required as "MAJOR.MINOR.PATCH"')
    major, minor, _patch = map(int, match.groups())
    if major != 2 or minor > 0:
        raise OcrError(
            f"{source}: unsupported schema_version {value}; supported 2.0.x. "
            "For 1.0.x, remove existing_text/redo_dpi, set schema_version to 2.0.0, "
            "and use --redo-ocr explicitly when replacing OCR text."
        )


def _merge(
    target: dict[str, Any],
    update: dict[str, Any],
    winners: dict[str, str],
    source: str,
    prefix: str = "",
) -> None:
    for key, value in update.items():
        dotted = prefix + key
        if isinstance(value, dict):
            _merge(target[key], value, winners, source, dotted + ".")
        else:
            target[key] = value
            winners[dotted] = source


def _path(value: str, cwd: Path) -> Path:
    expanded = Path(value).expanduser()
    return (expanded if expanded.is_absolute() else cwd / expanded).resolve()


def resolve_config(
    explicit: Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    cwd: Path | None = None,
) -> ResolvedConfig:
    cwd = (cwd or Path.cwd()).resolve()
    defaults: dict[str, Any] = yaml.safe_load(example_bytes())
    defaults.pop("schema_version")
    values = deepcopy(defaults)
    winners: dict[str, str] = {}
    _merge(values, defaults, winners, "built-in")
    sources: list[dict[str, Any]] = []
    candidates = [(user_config_path(), False), (cwd / ".tkn/config.yaml", False)]
    if explicit is not None:
        candidates.append((_path(str(explicit), cwd), True))
    for path, required in candidates:
        if not path.exists():
            if required:
                raise OcrError(f"Config file does not exist: {path}")
            continue
        try:
            mapping = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise OcrError(f"Cannot read YAML config: {path}") from exc
        if not isinstance(mapping, dict):
            raise OcrError(f"Config must be a mapping: {path}")
        version = mapping.pop("schema_version", None)
        _version(version, str(path))
        _validate(mapping, defaults, str(path))
        check = deepcopy(defaults)
        _merge(check, mapping, {}, str(path))
        _semantic(check)
        _merge(values, mapping, winners, str(path))
        sources.append({"path": str(path), "schema_version": version, "migration": False})
    if overrides:
        _validate(overrides, defaults, "CLI")
        _merge(values, overrides, winners, "CLI")
    _semantic(values)
    for key in ("input_dir", "output_dir", "state_dir"):
        if values[key] is not None:
            values[key] = str(_path(values[key], cwd))
    if values["azure"]["endpoint"]:
        values["azure"]["endpoint"] = values["azure"]["endpoint"].rstrip("/")
    config = Config(
        **{
            k: v
            for k, v in values.items()
            if k not in {"azure", "input_dir", "output_dir", "state_dir"}
        },
        input_dir=Path(values["input_dir"]) if values["input_dir"] else None,
        output_dir=Path(values["output_dir"]) if values["output_dir"] else None,
        state_dir=Path(values["state_dir"]),
        azure=AzureConfig(**values["azure"]),
    )
    return ResolvedConfig(config, values, sources, winners)


def _semantic(values: dict[str, Any]) -> None:
    if not 1 <= values["max_pages"] <= 2000 or not 1 <= values["max_file_mb"] <= 500:
        raise OcrError("max_pages must be 1..2000 and max_file_mb 1..500")
    azure = values["azure"]
    if azure["auth_mode"] == "azure_cli":
        raise OcrError("azure_cli was removed; set azure.auth_mode to browser and run auth login")
    if azure["auth_mode"] not in {"browser", "default_credential", "key"}:
        raise OcrError("azure.auth_mode must be browser, default_credential, or key")
    if azure["tenant_id"] is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.-]*", azure["tenant_id"]
    ):
        raise OcrError("azure.tenant_id must be a tenant ID or domain without a URL path")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", azure["key_env"]):
        raise OcrError("azure.key_env must be an environment variable name, not a key")
    if azure["api_version"] != "2024-11-30":
        raise OcrError("Only azure.api_version 2024-11-30 is supported")
    for key in ("request_timeout_seconds", "poll_interval_seconds", "poll_timeout_seconds"):
        if not 1 <= azure[key] <= 86400:
            raise OcrError(f"azure.{key} must be 1..86400 seconds")
    if not 0 <= azure["max_get_retries"] <= 10:
        raise OcrError("azure.max_get_retries must be 0..10")
    if azure["endpoint"]:
        try:
            url = urlsplit(azure["endpoint"])
            _ = url.port
        except ValueError as exc:
            raise OcrError("azure.endpoint is not a valid HTTPS origin") from exc
        if (
            url.scheme != "https"
            or not url.hostname
            or any(c.isspace() for c in azure["endpoint"])
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise OcrError(
                "azure.endpoint must be an HTTPS resource origin without path or secrets"
            )


def init_config(
    path: Path | None = None, *, force: bool = False, dry_run: bool = False
) -> dict[str, Any]:
    path = (path or user_config_path()).expanduser().resolve()
    data = example_bytes()
    existing = file_hash(path)
    if existing and path.read_bytes() == data:
        return {"status": "unchanged", "path": str(path)}
    if existing and not force:
        raise OcrError(f"Edited config exists: {path}; use --force to replace it with a backup")
    result: dict[str, Any] = {
        "status": "replaced" if existing else "created",
        "path": str(path),
        "dry_run": dry_run,
    }
    if not dry_run:
        if existing:
            result["backup"] = str(backup(path, existing))
        atomic_write(path, data, replace=existing is not None)
    return result
