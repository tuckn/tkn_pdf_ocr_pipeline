from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock, Timeout

from .azure import AzureRead, Provider
from .config import Config
from .errors import OcrError
from .io_utils import atomic_write, backup, file_hash, sha256, write_json
from .pdf import PdfInfo, inspect_pdf, merge_ocr, prepare_upload, select_pages, validate_output
from .text_json import build_text_json

LOGGER = logging.getLogger("pdf_ocr_pipeline")


@dataclass(frozen=True)
class Options:
    dry_run: bool = False
    overwrite: bool = False
    redo_ocr: bool = False
    json: bool = False
    only_json: bool = False
    json_output: Path | None = None


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def validate_roots(config: Config, source_root: Path, output_root: Path) -> None:
    json_root = config.json_output_dir or output_root
    for first, second, label in (
        (source_root, output_root, "Input and output"),
        (source_root, config.state_dir, "Input and state"),
        (output_root, config.state_dir, "Output and state"),
        (source_root, json_root, "Input and JSON output"),
        (json_root, config.state_dir, "JSON output and state"),
    ):
        if _within(first, second) or _within(second, first):
            raise OcrError(f"{label} directories must be separate and non-nested")


def discover(config: Config) -> list[tuple[Path, Path]]:
    if config.input_dir is None or config.output_dir is None:
        raise OcrError("Select a configured source with input_dir and output_dir")
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
                if config.source_id and candidate.is_symlink():
                    raise OcrError("Named queues do not accept symbolic-link PDF inputs")
                if not candidate.resolve().is_relative_to(root):
                    raise OcrError("Input file link resolves outside input_dir")
                paths.append(candidate)
    pairs = []
    for path in sorted(paths, key=lambda p: str(p).casefold()):
        relative = path.relative_to(root)
        output = config.output_dir / relative.with_name(
            relative.stem + config.output_suffix + relative.suffix
        )
        if not output.resolve().is_relative_to(config.output_dir):
            raise OcrError("Output link resolves outside output_dir")
        pairs.append((path, output))
    return pairs


def source_configs(config: Config, selected: str | None = None) -> list[Config]:
    if not config.sources:
        if selected:
            raise OcrError(f"Unknown source: {selected}")
        raise OcrError("Configure input_dir and output_dir under sources.<id> before running")
    if selected and (selected not in config.sources or not config.sources[selected].enabled):
        raise OcrError(f"Unknown or disabled source: {selected}")
    configs: list[Config] = []
    roots: list[Path] = [config.state_dir]
    for source_id, item in config.sources.items():
        if not item.enabled:
            continue
        if item.input_dir is None or item.output_dir is None:
            raise OcrError(f"sources.{source_id} requires input_dir and output_dir")
        json_root = item.json_output_dir or item.output_dir
        local = replace(
            config,
            input_dir=item.input_dir,
            output_dir=item.output_dir,
            json_output_dir=item.json_output_dir,
        )
        validate_roots(local, item.input_dir, item.output_dir)
        for root in dict.fromkeys((item.input_dir, item.output_dir, json_root)):
            if any(_within(root, other) or _within(other, root) for other in roots):
                raise OcrError("Enabled source roots must be separate and non-nested")
        roots.extend(dict.fromkeys((item.input_dir, item.output_dir, json_root)))
        configs.append(
            replace(
                config,
                sources={},
                source_id=source_id,
                recursive=item.recursive,
                input_dir=item.input_dir,
                output_dir=item.output_dir,
                json_output_dir=item.json_output_dir,
                after_success=item.after_success,
                output_suffix=item.output_suffix,
            )
        )
    return [item for item in configs if selected is None or item.source_id == selected]


def run_sources(
    config: Config,
    options: Options,
    selected: str | None = None,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> dict[str, Any]:
    configs = source_configs(config, selected)
    jobs = [(source, output, item) for item in configs for source, output in discover(item)]
    return _run_jobs(
        jobs,
        config,
        options,
        provider_factory=provider_factory,
    )


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


def _json_target(output: Path, config: Config, options: Options) -> Path | None:
    if not (options.json or options.only_json):
        return None
    if options.json_output is not None:
        target = options.json_output.expanduser().resolve()
    elif config.source_id and config.json_output_dir and config.output_dir:
        target = config.json_output_dir / output.relative_to(config.output_dir)
        target = target.with_suffix(".json").resolve()
    else:
        target = output.with_suffix(".json")
    if target.suffix.lower() != ".json":
        raise OcrError("JSON output filename must end in .json")
    if (
        config.source_id
        and config.json_output_dir
        and not target.is_relative_to(config.json_output_dir)
    ):
        raise OcrError("JSON output link resolves outside json_output_dir")
    return target


def process_file(
    source: Path,
    output: Path,
    config: Config,
    options: Options,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> dict[str, Any]:
    if config.source_id and source.is_symlink():
        raise OcrError("Named queues do not accept symbolic-link PDF inputs")
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".pdf":
        raise OcrError("Output filename must end in .pdf")
    json_path = _json_target(output, config, options)
    primary = json_path if options.only_json else output
    assert primary is not None
    if source == primary or (primary.exists() and source.exists() and source.samefile(primary)):
        raise OcrError("Source and output must be different files; in-place OCR is unsupported")
    if (
        _within(source, config.state_dir)
        or _within(primary, config.state_dir)
        or (json_path and _within(json_path, config.state_dir))
    ):
        raise OcrError("Input and output files cannot be inside state_dir")
    destinations = [primary.parent, config.state_dir]
    if json_path:
        destinations.append(json_path.parent)
    for destination in destinations:
        for ancestor in (destination, *destination.parents):
            if ancestor.exists():
                if not ancestor.is_dir():
                    raise OcrError(f"Destination ancestor is not a directory: {ancestor}")
                break
    options = replace(options, json_output=json_path)
    if options.dry_run:
        return _process_locked(source, output, config, options, provider_factory)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_key = sha256(os.path.normcase(str(primary)).encode())
    lock_dir = config.state_dir / "locks"
    lock_dir.mkdir(exist_ok=True)
    source_key = sha256(os.path.normcase(str(source)).encode())
    try:
        with (
            FileLock(lock_dir / f"source-{source_key}.lock", timeout=0),
            FileLock(lock_dir / f"{lock_key}.lock", timeout=0),
        ):
            return _process_locked(source, output, config, options, provider_factory)
    except Timeout as exc:
        raise OcrError(f"Another process is handling this input or output: {output}") from exc


def _process_locked(
    source: Path,
    output: Path,
    config: Config,
    options: Options,
    provider_factory: Callable[[], Provider] | None,
) -> dict[str, Any]:
    json_path = options.json_output
    primary = json_path if options.only_json else output
    assert primary is not None
    base: dict[str, Any] = {"source": str(source), "output": str(primary)}
    if json_path and not options.only_json:
        base["json_output"] = str(json_path)
    if config.source_id:
        base["source_id"] = config.source_id

    existing = file_hash(primary)
    existing_json = file_hash(json_path) if json_path and not options.only_json else None
    if not options.overwrite and (existing is not None or existing_json is not None):
        return {
            **base,
            "status": "skipped",
            "reason": "output_exists" if existing is not None else "json_output_exists",
            "source_action": "kept",
        }

    snapshot = _snapshot(source, config)
    if snapshot is None:
        return {**base, "status": "skipped", "reason": "input_not_stable_yet"}
    data, digest, info = snapshot
    if options.only_json:
        return _json_only_locked(
            source, primary, config, options, provider_factory, base, data, digest, info, existing
        )

    pages = select_pages(info, redo_ocr=options.redo_ocr)
    base.update(
        pages=info.pages, text_pages=info.text_pages, page_kinds=info.page_kinds, ocr_pages=pages
    )
    if config.source_id:
        unreadable = [
            n
            for n, kind in enumerate(info.page_kinds, 1)
            if kind in {"native_text", "ocr_text"} and n not in info.text_pages
        ]
        if "unsupported" in info.page_kinds or unreadable or (not pages and not info.text_pages):
            return {
                **base,
                "status": "needs_review",
                "reason": "input_not_searchable_safely",
                "source_action": "kept",
            }
    elif not pages and not options.json:
        return {**base, "status": "skipped", "reason": "no_eligible_pages"}

    if (pages or options.json) and not config.azure.endpoint:
        raise OcrError("Set azure.endpoint before OCR; use config init and config list")
    if (
        (pages or options.json)
        and config.azure.auth_mode == "key"
        and not os.environ.get(config.azure.key_env, "").strip()
    ):
        raise OcrError(f"Set environment variable {config.azure.key_env}")
    if options.dry_run:
        return {
            **base,
            "status": "planned",
            "action": "replaced" if existing else "created",
            "azure_action": "submit" if pages or options.json else "none",
            "method": "ocr" if pages else "copy",
            "source_action": config.after_success,
            "preserve_images": True,
        }
    if not pages and not options.json:
        return _publish(source, output, config, base, digest, data, info, existing, method="copy")

    analyzed_pages = pages or list(range(1, info.pages + 1))
    upload = prepare_upload(data, pages) if pages else data
    if len(upload) > config.max_file_mb * 1024 * 1024:
        raise OcrError("Prepared upload exceeds max_file_mb")
    upload_info = inspect_pdf(upload)
    if pages and upload_info.image_hashes != [info.image_hashes[n - 1] for n in pages]:
        raise OcrError("Image preservation check failed before upload")
    provider = provider_factory() if provider_factory else AzureRead(config.azure)
    try:
        provider.prepare()
        if file_hash(source) != digest:
            raise OcrError("Input changed before upload")
        LOGGER.info("Submitting %s (%s pages) to Azure", source.name, len(analyzed_pages))
        operation = provider.submit(upload, include_pdf=bool(pages))
        recognized, azure_text_pages, analysis = provider.collect(
            operation, include_pdf=bool(pages)
        )
        if pages:
            assert recognized is not None
            validate_output(recognized, upload_info, azure_text_pages)
            result = merge_ocr(data, recognized, pages)
            text_pages = [pages[n - 1] for n in azure_text_pages]
        else:
            result = data
            text_pages = []
        if config.source_id and options.json and not azure_text_pages:
            return {
                **base,
                "status": "needs_review",
                "reason": "json_without_text",
                "source_action": "kept",
            }
        result_info = validate_output(result, info, text_pages)
        if config.source_id and any(n not in result_info.text_pages for n in pages):
            return {
                **base,
                "status": "needs_review",
                "reason": "ocr_pages_without_text",
                "source_action": "kept",
            }
        json_data = (
            build_text_json(
                analysis,
                source_hash=digest,
                source_name=source.name,
                source_pages=info.pages,
                analyzed_pages=analyzed_pages,
                api_version=config.azure.api_version,
            )
            if options.json
            else None
        )
        return _publish(
            source,
            output,
            config,
            base,
            digest,
            result,
            result_info,
            existing,
            method="ocr" if pages else "copy",
            json_path=json_path,
            json_data=json_data,
            existing_json=existing_json,
        )
    finally:
        provider.close()


def _json_only_locked(
    source: Path,
    output: Path,
    config: Config,
    options: Options,
    provider_factory: Callable[[], Provider] | None,
    base: dict[str, Any],
    data: bytes,
    digest: str,
    info: PdfInfo,
    existing: str | None,
) -> dict[str, Any]:
    pages = list(range(1, info.pages + 1))
    base.update(pages=info.pages, analyzed_pages=pages, method="json")
    if not config.azure.endpoint:
        raise OcrError("Set azure.endpoint before OCR; use config init and config list")
    if config.azure.auth_mode == "key" and not os.environ.get(config.azure.key_env, "").strip():
        raise OcrError(f"Set environment variable {config.azure.key_env}")
    if options.dry_run:
        return {
            **base,
            "status": "planned",
            "action": "replaced" if existing else "created",
            "azure_action": "submit",
            "source_action": config.after_success,
        }
    provider = provider_factory() if provider_factory else AzureRead(config.azure)
    try:
        provider.prepare()
        if file_hash(source) != digest:
            raise OcrError("Input changed before upload")
        operation = provider.submit(data, include_pdf=False)
        _pdf, text_pages, analysis = provider.collect(operation, include_pdf=False)
        if config.source_id and any(page not in text_pages for page in pages):
            return {
                **base,
                "status": "needs_review",
                "reason": "json_pages_without_text",
                "source_action": "kept",
            }
        result = build_text_json(
            analysis,
            source_hash=digest,
            source_name=source.name,
            source_pages=info.pages,
            analyzed_pages=pages,
            api_version=config.azure.api_version,
        )
        return _publish_bytes(
            source,
            output,
            config,
            base,
            digest,
            result,
            existing,
            output_text_pages=text_pages,
            method="json",
        )
    finally:
        provider.close()


def _publish(
    source: Path,
    output: Path,
    config: Config,
    base: dict[str, Any],
    digest: str,
    result: bytes,
    result_info: PdfInfo,
    existing: str | None,
    *,
    method: str,
    json_path: Path | None = None,
    json_data: bytes | None = None,
    existing_json: str | None = None,
) -> dict[str, Any]:
    if not result_info.text_pages:
        LOGGER.warning(
            "OCR returned no extractable text: %s; inspect blank/illegible pages", source.name
        )
    return _publish_bytes(
        source,
        output,
        config,
        base,
        digest,
        result,
        existing,
        output_text_pages=result_info.text_pages,
        method=method,
        json_path=json_path,
        json_data=json_data,
        existing_json=existing_json,
    )


def _publish_bytes(
    source: Path,
    output: Path,
    config: Config,
    base: dict[str, Any],
    digest: str,
    result: bytes,
    existing: str | None,
    *,
    output_text_pages: list[int],
    method: str,
    json_path: Path | None = None,
    json_data: bytes | None = None,
    existing_json: str | None = None,
) -> dict[str, Any]:
    if file_hash(source) != digest:
        raise OcrError("Input changed during OCR; result was not published")
    if file_hash(output) != existing:
        raise OcrError("Output changed during OCR; result was not published")
    if json_path and file_hash(json_path) != existing_json:
        raise OcrError("JSON output changed during OCR; result was not published")
    result_digest = sha256(result)
    json_digest = sha256(json_data) if json_data is not None else None
    backup_path = backup(output, existing) if existing else None
    json_backup = backup(json_path, existing_json) if json_path and existing_json else None
    if file_hash(source) != digest or file_hash(output) != existing:
        raise OcrError("Input or output changed before publication")
    if json_path and file_hash(json_path) != existing_json:
        raise OcrError("JSON output changed before publication")
    if json_path and json_data is not None:
        atomic_write(json_path, json_data, replace=existing_json is not None)
        if file_hash(json_path) != json_digest:
            raise OcrError("Saved JSON output is missing or changed; input retained")
    if file_hash(output) != existing:
        raise OcrError("Output changed before publication")
    atomic_write(output, result, replace=existing is not None)
    if file_hash(output) != result_digest:
        raise OcrError("Saved output is missing or changed; input retained")
    if json_path and file_hash(json_path) != json_digest:
        raise OcrError("Saved JSON output is missing or changed; input retained")
    published = {
        **base,
        "status": "replaced" if existing else "created",
        "output_sha256": result_digest,
        "output_text_pages": output_text_pages,
        "method": method,
    }
    if backup_path:
        published["backup"] = str(backup_path)
    if json_digest:
        published["json_sha256"] = json_digest
    if json_backup:
        published["json_backup"] = str(json_backup)
    if config.source_id:
        published["source_action"] = "kept"
        if config.after_success == "delete":
            if file_hash(source) != digest:
                return {
                    **published,
                    "status": "needs_review",
                    "reason": "input_changed_before_delete",
                }
            if file_hash(output) != result_digest or (
                json_path and file_hash(json_path) != json_digest
            ):
                return {
                    **published,
                    "status": "needs_review",
                    "reason": "output_changed_before_delete",
                }
            try:
                source.unlink()
            except OSError:
                return {**published, "status": "needs_review", "reason": "input_delete_failed"}
            published["source_action"] = "deleted"
    return published


def run(
    pairs: list[tuple[Path, Path]],
    config: Config,
    options: Options,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> dict[str, Any]:
    return _run_jobs(
        [(source, output, config) for source, output in pairs],
        config,
        options,
        provider_factory=provider_factory,
    )


def _run_jobs(
    jobs: list[tuple[Path, Path, Config]],
    config: Config,
    options: Options,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, (source, output, item) in enumerate(jobs, 1):
        LOGGER.info("Processing %s/%s: %s", index, len(jobs), source.name)
        try:
            result = process_file(source, output, item, options, provider_factory=provider_factory)
        except (OcrError, OSError) as exc:
            message = str(exc) if isinstance(exc, OcrError) else f"File access failed: {source}"
            LOGGER.error("%s", message)
            result = {
                "source": str(source),
                "output": str(output),
                "status": "failed",
                "error": message,
            }
        if item.source_id:
            result["source_id"] = item.source_id
        results.append(result)
    counts = {
        status: sum(r["status"] == status for r in results)
        for status in (
            "created",
            "replaced",
            "skipped",
            "planned",
            "failed",
            "needs_review",
        )
    }
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "dry_run": options.dry_run,
        "counts": counts,
        "files": results,
        "sources": {
            source_id: {
                status: sum(
                    r["status"] == status and r.get("source_id") == source_id for r in results
                )
                for status in counts
            }
            for source_id in sorted({r["source_id"] for r in results if r.get("source_id")})
        },
    }
    if not options.dry_run:
        path = config.state_dir / "runs" / f"{uuid4().hex}.json"
        report["run_report"] = str(path)
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(path, report)
    return report
