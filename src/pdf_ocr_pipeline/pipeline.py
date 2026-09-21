from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock, Timeout

from . import __version__
from .azure import MODEL_ID, AzureRead, Provider
from .config import Config
from .errors import OcrError
from .io_utils import atomic_write, backup, file_hash, read_json, sha256, write_json
from .pdf import PdfInfo, inspect_pdf, merge_ocr, prepare_upload, select_pages, validate_output

LOGGER = logging.getLogger("pdf_ocr_pipeline")
# Image-preserving composition must never reuse the earlier rasterized output.
PDF_PROCESSING_VERSION = "0.3.0"


@dataclass(frozen=True)
class Options:
    dry_run: bool = False
    overwrite: bool = False
    retry_uncertain: bool = False
    redo_ocr: bool = False


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def validate_roots(config: Config, source_root: Path, output_root: Path) -> None:
    for first, second, label in (
        (source_root, output_root, "Input and output"),
        (source_root, config.state_dir, "Input and state"),
        (output_root, config.state_dir, "Output and state"),
    ):
        if _within(first, second) or _within(second, first):
            raise OcrError(f"{label} directories must be separate and non-nested")


def discover(config: Config) -> list[tuple[Path, Path]]:
    if config.input_dir is None or config.output_dir is None:
        raise OcrError("Set input_dir and output_dir, or pass --input-dir and --output-dir")
    root = config.input_dir
    if not root.is_dir():
        raise OcrError(f"Input directory does not exist: {root}")
    validate_roots(config, root, config.output_dir)
    paths: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise OcrError(f"Cannot enumerate input directory: {error.filename}")

    for folder, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = (
            sorted(
                name
                for name in dirs
                if not Path(folder, name).is_symlink()
                and not getattr(Path(folder, name), "is_junction", lambda: False)()
            )
            if config.recursive
            else []
        )
        for name in names:
            candidate = Path(folder, name)
            if candidate.suffix.lower() == ".pdf":
                if not candidate.resolve().is_relative_to(root):
                    raise OcrError("Input file link resolves outside input_dir")
                paths.append(candidate)
    pairs = []
    for path in sorted(paths, key=lambda p: str(p).casefold()):
        output = config.output_dir / path.relative_to(root)
        if not output.resolve().is_relative_to(config.output_dir):
            raise OcrError("Output link resolves outside output_dir")
        pairs.append((path, output))
    return pairs


def _fingerprint(config: Config, redo_ocr: bool = False) -> str:
    conditions = {
        "generator": PDF_PROCESSING_VERSION,
        "model": MODEL_ID,
        "endpoint": config.azure.endpoint,
        "api_version": config.azure.api_version,
        "locale": config.azure.locale,
        "redo_ocr": redo_ocr,
    }
    return sha256(json.dumps(conditions, sort_keys=True).encode())


def _snapshot(source: Path, config: Config) -> tuple[bytes, str, PdfInfo] | None:
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise OcrError(f"Input must be an existing .pdf file: {source}")
    before = source.stat()
    if time.time() - before.st_mtime < config.min_age_seconds:
        return None
    if before.st_size > config.max_file_mb * 1024 * 1024:
        raise OcrError("Input exceeds max_file_mb")
    data = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OcrError("Input changed while being read; retry after the writer finishes")
    info = inspect_pdf(data)
    if info.pages > config.max_pages:
        raise OcrError("Input exceeds max_pages")
    return data, sha256(data), info


def _identity(source: Path, output: Path, digest: str, fingerprint: str) -> dict[str, str]:
    return {
        "source": str(source),
        "output": str(output),
        "source_sha256": digest,
        "fingerprint": fingerprint,
    }


def _check_state(state: dict[str, Any], identity: dict[str, str]) -> None:
    if any(state.get(k) != v for k, v in identity.items()):
        raise OcrError("State identity does not match this job; preserve state and investigate")
    if state.get("status") not in {"submitting", "submitted", "ready", "completed"}:
        raise OcrError("Unsupported job state; preserve state and investigate")
    if state["status"] != "submitting" and not isinstance(state.get("operation_url"), str):
        raise OcrError("Job state is missing its Azure operation URL")
    if state["status"] in {"ready", "completed"} and not isinstance(
        state.get("output_sha256"), str
    ):
        raise OcrError("Job state is missing its output hash")


def process_file(
    source: Path,
    output: Path,
    config: Config,
    options: Options,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".pdf":
        raise OcrError("Output filename must end in .pdf")
    if source == output or (output.exists() and source.exists() and source.samefile(output)):
        raise OcrError("Source and output must be different files; in-place OCR is unsupported")
    if _within(source, config.state_dir) or _within(output, config.state_dir):
        raise OcrError("Input and output files cannot be inside state_dir")
    for destination in (output.parent, config.state_dir):
        for ancestor in (destination, *destination.parents):
            if ancestor.exists():
                if not ancestor.is_dir():
                    raise OcrError(f"Destination ancestor is not a directory: {ancestor}")
                break
    if options.dry_run:
        return _process_locked(source, output, config, options, provider_factory)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_key = sha256(os.path.normcase(str(output)).encode())
    lock_dir = config.state_dir / "locks"
    lock_dir.mkdir(exist_ok=True)
    try:
        with FileLock(lock_dir / f"{lock_key}.lock", timeout=0):
            return _process_locked(source, output, config, options, provider_factory)
    except Timeout as exc:
        raise OcrError(f"Another process is handling this output: {output}") from exc


def _process_locked(
    source: Path,
    output: Path,
    config: Config,
    options: Options,
    provider_factory: Callable[[], Provider] | None,
) -> dict[str, Any]:
    base: dict[str, Any] = {"source": str(source), "output": str(output)}
    snapshot = _snapshot(source, config)
    if snapshot is None:
        return {**base, "status": "skipped", "reason": "input_not_stable_yet"}
    data, digest, info = snapshot
    pages = select_pages(info, redo_ocr=options.redo_ocr)
    base.update(
        {
            "pages": info.pages,
            "text_pages": info.text_pages,
            "page_kinds": info.page_kinds,
            "ocr_pages": pages,
        }
    )
    if not pages:
        return {**base, "status": "skipped", "reason": "no_eligible_pages"}
    identity = _identity(source, output, digest, _fingerprint(config, options.redo_ocr))
    job_id = sha256(json.dumps(identity, sort_keys=True).encode())
    state_path = config.state_dir / "jobs" / f"{job_id}.json"
    state = read_json(state_path)
    if state:
        _check_state(state, identity)
    existing = file_hash(output)
    if (
        state
        and state["status"] in {"ready", "completed"}
        and existing == state.get("output_sha256")
    ):
        return {**base, "status": "unchanged", "state": str(state_path)}
    if existing is not None and not options.overwrite:
        raise OcrError(
            f"Output exists and differs or has no matching state: {output}; use --overwrite"
        )
    if state and state["status"] == "submitting" and not options.retry_uncertain:
        raise OcrError(
            f"Submission acceptance is uncertain; inspect {state_path} before --retry-uncertain "
            "(resubmission may incur another charge)"
        )
    if not config.azure.endpoint:
        raise OcrError("Set azure.endpoint before OCR; use config init and config show")
    if config.azure.auth_mode == "key" and not os.environ.get(config.azure.key_env, "").strip():
        raise OcrError(f"Set environment variable {config.azure.key_env}")
    if options.dry_run:
        return {
            **base,
            "status": "planned",
            "action": "replaced" if existing else "created",
            "azure_action": "resume" if state and not options.retry_uncertain else "submit",
            "preserve_images": True,
            "state": str(state_path),
        }
    upload = prepare_upload(data, pages)
    if len(upload) > config.max_file_mb * 1024 * 1024:
        raise OcrError("Prepared upload exceeds max_file_mb")
    upload_info = inspect_pdf(upload)
    if upload_info.image_hashes != [info.image_hashes[n - 1] for n in pages]:
        raise OcrError("Image preservation check failed before upload")
    provider = provider_factory() if provider_factory else AzureRead(config.azure)
    try:
        provider.prepare()
        if state is None or options.retry_uncertain:
            if file_hash(source) != digest:
                raise OcrError("Input changed before upload")
            state = {
                "schema_version": "1.0.0",
                **identity,
                "status": "submitting",
                "generator_version": __version__,
                "model_id": MODEL_ID,
                "api_version": config.azure.api_version,
                "pages": info.pages,
                "ocr_pages": pages,
                "source_bytes": len(data),
                "upload_sha256": sha256(upload),
                "started_at": datetime.now(UTC).isoformat(),
            }
            # Durable intent before the billable POST closes the accidental resubmission gap.
            write_json(state_path, state)
            LOGGER.info("Submitting %s (%s pages) to Azure", source.name, len(pages))
            operation = provider.submit(upload)
            state.update(status="submitted", operation_url=operation)
            write_json(state_path, state)
        else:
            LOGGER.info("Resuming the saved Azure operation for %s", source.name)
        recognized, azure_text_pages = provider.collect(state["operation_url"])
        validate_output(recognized, upload_info, azure_text_pages)
        result = merge_ocr(data, recognized, pages)
        text_pages = [pages[n - 1] for n in azure_text_pages]
        result_info = validate_output(result, info, text_pages)
        if file_hash(source) != digest:
            raise OcrError("Input changed during OCR; result was not published")
        if file_hash(output) != existing:
            raise OcrError("Output changed during OCR; result was not published")
        if not result_info.text_pages:
            LOGGER.warning(
                "OCR returned no extractable text: %s; inspect blank/illegible pages", source.name
            )
        state.update(
            status="ready",
            output_sha256=sha256(result),
            output_text_pages=result_info.text_pages,
            ocr_text_pages=text_pages,
        )
        write_json(state_path, state)
        backup_path = backup(output, existing) if existing else None
        if file_hash(output) != existing:
            raise OcrError("Output changed before publication")
        atomic_write(output, result, replace=existing is not None)
        state.update(status="completed", completed_at=datetime.now(UTC).isoformat())
        if backup_path:
            state["backup"] = str(backup_path)
        write_json(state_path, state)
        return {
            **base,
            "status": "replaced" if existing else "created",
            "state": str(state_path),
            "backup": str(backup_path) if backup_path else None,
            "output_sha256": sha256(result),
            "output_text_pages": result_info.text_pages,
        }
    finally:
        provider.close()


def run(
    pairs: list[tuple[Path, Path]],
    config: Config,
    options: Options,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, (source, output) in enumerate(pairs, 1):
        LOGGER.info("Processing %s/%s: %s", index, len(pairs), source.name)
        try:
            result = process_file(
                source, output, config, options, provider_factory=provider_factory
            )
        except (OcrError, OSError) as exc:
            message = str(exc) if isinstance(exc, OcrError) else f"File access failed: {source}"
            LOGGER.error("%s", message)
            result = {
                "source": str(source),
                "output": str(output),
                "status": "failed",
                "error": message,
            }
        results.append(result)
    counts = {
        status: sum(r["status"] == status for r in results)
        for status in ("created", "replaced", "unchanged", "skipped", "planned", "failed")
    }
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "dry_run": options.dry_run,
        "counts": counts,
        "files": results,
    }
    if not options.dry_run:
        path = config.state_dir / "runs" / f"{uuid4().hex}.json"
        report["run_report"] = str(path)
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(path, report)
    return report
