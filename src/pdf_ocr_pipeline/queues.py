from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .config import Config
from .errors import OcrError
from .io_utils import file_hash, read_json, sha256, write_json


def handoff_path(config: Config, source: Path, digest: str) -> Path:
    # Delivery identity deliberately excludes output location and OCR settings.
    key = sha256((os.path.normcase(str(source)) + "\n" + digest).encode())
    assert config.source_id is not None
    return config.state_dir / "handoffs" / config.source_id / f"{key}.json"


def read_handoff(path: Path, config: Config, source: Path, digest: str) -> dict[str, Any] | None:
    record = read_json(path)
    if record is None:
        return None
    if (
        record.get("source_id") != config.source_id
        or record.get("source") != str(source)
        or record.get("source_sha256") != digest
        or record.get("status") not in {"publishing", "published", "cleanup_pending", "completed"}
        or not isinstance(record.get("output"), str)
        or not Path(record["output"]).is_absolute()
        or not isinstance(record.get("output_sha256"), str)
        or record.get("source_action") not in {"pending", "kept", "deleted"}
    ):
        raise OcrError("Invalid handoff record; preserve state and investigate")
    return record


def finish_handoff(
    path: Path,
    record: dict[str, Any],
    config: Config,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "handoff": str(path),
        "output": record["output"],
        "output_sha256": record["output_sha256"],
    }
    # Completed delivery is durable even if the downstream consumer moved the PDF.
    if record["status"] == "completed" and (
        record["source_action"] == "deleted" or config.after_success == "keep"
    ):
        return {
            **base,
            "status": "unchanged",
            "reason": "already_handed_off",
            "source_action": "kept",
            "recorded_source_action": record["source_action"],
        }
    if file_hash(Path(record["output"])) != record["output_sha256"]:
        return {
            **base,
            "status": "needs_review",
            "reason": "handoff_output_missing_or_changed",
            "source_action": "kept",
        }
    source = Path(record["source"])
    digest = file_hash(source)
    if digest != record["source_sha256"]:
        return {
            **base,
            "status": "needs_review",
            "reason": "input_changed_before_cleanup",
            "source_action": "kept",
        }
    if dry_run:
        return {
            **base,
            "status": "planned",
            "action": "finish_handoff",
            "azure_action": "none",
            "source_action": "delete" if config.after_success == "delete" else "keep",
        }
    if config.after_success == "delete":
        record.update(status="cleanup_pending", source_action="pending")
        write_json(path, record)
        try:
            # Recheck after persisting intent. Cooperating runs also hold the source lock.
            if file_hash(Path(record["output"])) != record["output_sha256"]:
                return {
                    **base,
                    "status": "needs_review",
                    "reason": "handoff_output_missing_or_changed",
                    "source_action": "kept",
                }
            if source.is_symlink() or file_hash(source) != record["source_sha256"]:
                return {
                    **base,
                    "status": "needs_review",
                    "reason": "input_changed_before_cleanup",
                    "source_action": "kept",
                }
            source.unlink()
        except OSError:
            return {
                **base,
                "status": "cleanup_pending",
                "reason": "input_delete_failed",
                "source_action": "kept",
            }
        record["source_action"] = "deleted"
    else:
        record["source_action"] = "kept"
    record.update(status="completed", completed_at=datetime.now(UTC).isoformat())
    write_json(path, record)
    return {
        **base,
        "status": "unchanged",
        "reason": "handoff_completed",
        "source_action": record["source_action"],
    }


def publish_handoff(
    path: Path,
    config: Config,
    source: Path,
    output: Path,
    digest: str,
    result_digest: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "1.0.0",
        "source_id": config.source_id,
        "source": str(source),
        "source_sha256": digest,
        "output": str(output),
        "output_sha256": result_digest,
        "validation": "passed",
        "status": "publishing",
        "source_action": "pending",
        "started_at": datetime.now(UTC).isoformat(),
    }
    # Persist before making the final filename visible to a downstream consumer.
    write_json(path, record)
    return record


def reconcile_handoffs(configs: list[Config], *, dry_run: bool) -> list[dict[str, Any]]:
    """Finish a recorded deletion interrupted after unlink but before its final record."""
    results: list[dict[str, Any]] = []
    for config in configs:
        if not config.source_id:
            continue
        root = config.state_dir / "handoffs" / config.source_id
        for path in sorted(root.glob("*.json")):
            record = read_json(path)
            if not record or record.get("status") not in {
                "publishing",
                "published",
                "cleanup_pending",
            }:
                continue
            source_value = record.get("source")
            if not isinstance(source_value, str):
                raise OcrError("Invalid handoff record; preserve state and investigate")
            source = Path(source_value)
            digest = record.get("source_sha256", "")
            if not source.is_absolute() or not isinstance(digest, str):
                raise OcrError("Invalid handoff record; preserve state and investigate")
            record = read_handoff(path, config, source, digest)
            assert record is not None
            if source.exists():
                continue
            base: dict[str, Any] = {
                "source_id": config.source_id,
                "source": str(source),
                "output": record["output"],
                "handoff": str(path),
            }
            if record["status"] != "cleanup_pending":
                results.append(
                    {**base, "status": "needs_review", "reason": "input_missing_before_cleanup"}
                )
                continue
            if dry_run:
                results.append(
                    {
                        **base,
                        "status": "planned",
                        "action": "record_input_absent",
                        "source_action": "none",
                        "azure_action": "none",
                    }
                )
                continue
            key = sha256(os.path.normcase(str(source)).encode())
            lock_dir = config.state_dir / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            try:
                with FileLock(lock_dir / f"source-{key}.lock", timeout=0):
                    record = read_handoff(path, config, source, digest)
                    if record is None or record["status"] != "cleanup_pending" or source.exists():
                        continue
                    record.update(
                        status="completed",
                        source_action="deleted",
                        completed_at=datetime.now(UTC).isoformat(),
                        recovery="input_absent_after_cleanup_intent",
                    )
                    write_json(path, record)
                    results.append(
                        {
                            **base,
                            "status": "unchanged",
                            "reason": "cleanup_record_recovered",
                            "source_action": "absent",
                        }
                    )
            except Timeout:
                results.append({**base, "status": "cleanup_pending", "reason": "source_locked"})
    return results
