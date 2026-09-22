import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from conftest import FakeProvider, make_pdf

from pdf_ocr_pipeline.cli import main
from pdf_ocr_pipeline.config import resolve_config
from pdf_ocr_pipeline.errors import OcrError
from pdf_ocr_pipeline.io_utils import sha256
from pdf_ocr_pipeline.pdf import inspect_pdf
from pdf_ocr_pipeline.pipeline import Options, discover, run_sources, source_configs


def queued(tmp_path, *, after="delete", data=None):
    settings = {
        "state_dir": str(tmp_path / "state"),
        "min_age_seconds": 0,
        "sources": {
            "receipts": {
                "input_dir": str(tmp_path / "in"),
                "output_dir": str(tmp_path / "out"),
                "after_success": after,
                "output_suffix": "_ocr",
            },
        },
        "azure": {
            "endpoint": "https://example.cognitiveservices.azure.com",
            "auth_mode": "key",
            "key_env": "TEST_OCR_KEY",
        },
    }
    config = resolve_config(overrides=settings).config
    source = tmp_path / "in/receipt.pdf"
    source.parent.mkdir()
    source.write_bytes(data if data is not None else make_pdf())
    return config, source, tmp_path / "out/receipt_ocr.pdf"


def only_result(config, *, dry=False, provider=None):
    return run_sources(
        config,
        Options(dry_run=dry),
        provider_factory=(
            (lambda: provider) if provider else lambda: pytest.fail("unexpected Azure access")
        ),
    )["files"][0]


def test_delete_only_after_verified_publication(tmp_path):
    config, source, output = queued(tmp_path)
    original = source.read_bytes()
    result = only_result(config, provider=FakeProvider())
    assert result["status"] == "created"
    assert result["source_action"] == "deleted"
    assert not source.exists()
    assert inspect_pdf(output.read_bytes()).image_hashes == inspect_pdf(original).image_hashes
    record = json.loads(Path(result["handoff"]).read_text())
    assert record["status"] == "completed" and record["validation"] == "passed"
    assert record["source_sha256"] == sha256(original)
    assert record["output_sha256"] == sha256(output.read_bytes())
    output.unlink()
    # Restoring an identical input does not cause duplicate delivery or another deletion.
    source.write_bytes(original)
    again = only_result(config)
    assert again["reason"] == "already_handed_off" and source.exists() and not output.exists()


def test_keep_completed_delivery_survives_output_move_and_setting_change(tmp_path):
    config, source, output = queued(tmp_path, after="keep")
    first = only_result(config, provider=FakeProvider())
    assert first["source_action"] == "kept"
    output.rename(tmp_path / "downstream.pdf")
    items = {"receipts": replace(config.sources["receipts"], output_suffix="_new")}
    config = replace(config, sources=items, azure=replace(config.azure, locale="ja"))
    again = only_result(config)
    assert again["reason"] == "already_handed_off"
    assert again["output"] == str(output.resolve())
    assert source.exists() and not output.exists()


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("after", ["keep", "delete"])
def test_searchable_pdf_passes_through_without_azure(tmp_path, hidden, after):
    original = make_pdf("already searchable", hidden=hidden)
    config, source, output = queued(tmp_path, after=after, data=original)
    config = replace(config, azure=replace(config.azure, endpoint=None))
    first = only_result(config)
    assert first["method"] == "copy" and first["status"] == "created"
    assert output.read_bytes() == original
    assert source.exists() == (after == "keep")


@pytest.mark.parametrize("data", [make_pdf(), make_pdf("existing", hidden=True)])
def test_dry_run_previews_delete_and_writes_nothing(tmp_path, data):
    config, source, output = queued(tmp_path, data=data)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    dirs = set(tmp_path.rglob("*"))
    result = only_result(config, dry=True)
    assert result["status"] == "planned" and result["source_action"] == "delete"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert set(tmp_path.rglob("*")) == dirs and not output.exists()


def block_delete(monkeypatch, source):
    real = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == source.resolve():
            raise PermissionError("synthetic sharing violation")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    return real


def test_failed_delete_retries_cleanup_without_provider(tmp_path, monkeypatch):
    config, source, output = queued(tmp_path)
    real = block_delete(monkeypatch, source)
    first = only_result(config, provider=FakeProvider())
    assert first["status"] == "cleanup_pending" and source.exists() and output.exists()
    state = json.loads(Path(first["handoff"]).read_text())
    assert state["status"] == "cleanup_pending"
    planned = only_result(config, dry=True)
    assert planned["action"] == "finish_handoff" and source.exists()
    assert json.loads(Path(first["handoff"]).read_text()) == state
    monkeypatch.setattr(Path, "unlink", real)
    again = only_result(config)
    assert again["source_action"] == "deleted" and not source.exists()


@pytest.mark.parametrize("change", ["missing", "changed"])
def test_pending_cleanup_never_deletes_without_same_output(tmp_path, monkeypatch, change):
    config, source, output = queued(tmp_path)
    real = block_delete(monkeypatch, source)
    only_result(config, provider=FakeProvider())
    monkeypatch.setattr(Path, "unlink", real)
    if change == "missing":
        output.unlink()
    else:
        output.write_bytes(b"changed downstream")
    result = only_result(config)
    assert result["status"] == "needs_review"
    assert result["reason"] == "handoff_output_missing_or_changed" and source.exists()
    assert output.exists() == (change == "changed")


def test_input_changed_after_publication_is_retained(tmp_path, monkeypatch):
    import pdf_ocr_pipeline.pipeline as module

    config, source, output = queued(tmp_path)
    original = module.write_json

    def changed(path, value):
        original(path, value)
        if value.get("status") == "published":
            source.write_bytes(make_pdf("new source"))

    monkeypatch.setattr(module, "write_json", changed)
    result = only_result(config, provider=FakeProvider())
    assert result["status"] == "needs_review"
    assert result["reason"] == "input_changed_before_cleanup"
    assert source.exists() and output.exists()


@pytest.mark.parametrize("failure", ["before_write", "after_write", "downstream_move"])
def test_ambiguous_publication_never_recreates_or_deletes(tmp_path, monkeypatch, failure):
    import pdf_ocr_pipeline.pipeline as module

    config, source, output = queued(tmp_path)
    real = module.atomic_write

    def publish(path, data, **kwargs):
        if failure != "before_write":
            real(path, data, **kwargs)
        if failure == "downstream_move":
            path.rename(tmp_path / "downstream.pdf")
        else:
            raise OSError("synthetic interruption")

    monkeypatch.setattr(module, "atomic_write", publish)
    first = only_result(config, provider=FakeProvider())
    assert first["status"] == "failed" and source.exists()
    monkeypatch.setattr(module, "atomic_write", real)
    again = only_result(config)
    if failure == "after_write":
        assert again["source_action"] == "deleted" and not source.exists()
    else:
        assert again["status"] == "needs_review" and source.exists() and not output.exists()


def test_interruption_after_delete_recovers_record(tmp_path, monkeypatch):
    import pdf_ocr_pipeline.queues as module

    config, source, output = queued(tmp_path)
    real = module.write_json

    def write(path, value):
        if value.get("status") == "completed":
            raise OSError("synthetic interruption after unlink")
        real(path, value)

    monkeypatch.setattr(module, "write_json", write)
    first = only_result(config, provider=FakeProvider())
    assert first["status"] == "failed" and not source.exists() and output.exists()
    monkeypatch.setattr(module, "write_json", real)
    planned = only_result(config, dry=True)
    assert planned["action"] == "record_input_absent"
    result = only_result(config)
    assert result["reason"] == "cleanup_record_recovered"
    assert json.loads(Path(result["handoff"]).read_text())["status"] == "completed"


def test_no_recognized_words_requires_review_and_no_retry(tmp_path):
    class NoWords(FakeProvider):
        def collect(self, operation_url):
            return make_pdf(), []

    config, source, output = queued(tmp_path)
    first = only_result(config, provider=NoWords())
    assert first["reason"] == "ocr_pages_without_text"
    assert first["status"] == "needs_review" and source.exists() and not output.exists()
    assert only_result(config)["status"] == "needs_review"


def test_blank_and_unsupported_inputs_retained(tmp_path):
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject

    config, source, output = queued(tmp_path, data=make_pdf(image=False))
    assert only_result(config)["status"] == "needs_review"
    writer = PdfWriter(clone_from=PdfReader(BytesIO(make_pdf())))
    writer.pages[0][NameObject("/StructParents")] = NumberObject(0)
    stream = BytesIO()
    writer.write(stream)
    source.write_bytes(stream.getvalue())
    assert only_result(config)["status"] == "needs_review"
    assert source.exists() and not output.exists()


def test_multiple_queues_selection_reports_and_failed_file_continuation(tmp_path):
    config, source, output = queued(tmp_path, data=b"invalid PDF")
    item = replace(
        config.sources["receipts"],
        input_dir=tmp_path / "cards-in",
        output_dir=tmp_path / "cards-out",
    )
    item.input_dir.mkdir()
    other = item.input_dir / "receipt.pdf"
    other.write_bytes(make_pdf("native text"))
    config = replace(config, sources={**config.sources, "cards": item})
    report = run_sources(config, Options(), provider_factory=lambda: pytest.fail("no Azure"))
    assert report["counts"]["failed"] == 1 and report["counts"]["created"] == 1
    assert report["sources"]["cards"]["created"] == 1
    assert report["sources"]["receipts"]["failed"] == 1
    assert source.exists() and not other.exists()
    assert (item.output_dir / "receipt_ocr.pdf").exists() and not output.exists()
    assert len(source_configs(config, "cards")) == 1
    with pytest.raises(OcrError, match="disabled"):
        source_configs(config, "missing")


@pytest.mark.parametrize("cross", ["same_input", "nested_output", "output_to_input", "state"])
def test_cross_queue_roots_rejected_even_when_selecting_one(tmp_path, cross):
    config, _, _ = queued(tmp_path)
    one = config.sources["receipts"]
    item = replace(one, input_dir=tmp_path / "b-in", output_dir=tmp_path / "b-out")
    if cross == "same_input":
        item = replace(item, input_dir=one.input_dir)
    elif cross == "nested_output":
        item = replace(item, output_dir=one.output_dir / "sub")
    elif cross == "output_to_input":
        item = replace(item, input_dir=one.output_dir)
    else:
        item = replace(item, input_dir=config.state_dir / "nested")
    config = replace(config, sources={**config.sources, "other": item})
    with pytest.raises(OcrError, match="non-nested"):
        run_sources(config, Options(dry_run=True), "receipts")
    assert not config.state_dir.exists()


def test_sources_config_layers_and_defaults(tmp_path):
    path = tmp_path / "home/config.yaml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0.0",
                "sources": {
                    "receipts": {"input_dir": "incoming", "output_dir": "outgoing"},
                    "disabled": {"enabled": False},
                },
            }
        )
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        'schema_version: "3.0.0"\nsources:\n  receipts:\n    after_success: delete\n'
    )
    resolved = resolve_config(overlay)
    assert resolved.config.sources["receipts"].input_dir == (tmp_path / "incoming").resolve()
    assert resolved.config.sources["receipts"].after_success == "delete"
    assert resolved.config.sources["receipts"].output_suffix == ""
    assert resolved.winning_sources["sources.receipts.after_success"] == str(overlay.resolve())
    assert resolved.winning_sources["sources.receipts.output_suffix"] == "built-in"
    assert len(source_configs(resolved.config)) == 1


@pytest.mark.parametrize(
    "sources",
    [
        [],
        {"bad/id": {}},
        {"CON": {}},
        {"con": {}},
        {"com1": {}},
        {"a": {"after_success": "remove"}},
        {"a": {"enabled": "true"}},
        {"a": {"output_suffix": "/escape"}},
        {"a": {"output_suffix": "x:y"}},
        {"a": {"unknown": True}},
        {"a": None},
    ],
)
def test_invalid_source_settings_rejected(sources):
    with pytest.raises(OcrError):
        resolve_config(overrides={"sources": sources})


def test_missing_queue_paths_rejected_and_default_source_available():
    incomplete = resolve_config(overrides={"sources": {"empty": {}}}).config
    with pytest.raises(OcrError, match="requires"):
        source_configs(incomplete)
    assert source_configs(resolve_config().config)[0].source_id == "default"


def test_cli_source_option_and_review_exit_code(tmp_path, capsys):
    config, source, _ = queued(tmp_path, data=make_pdf(image=False))
    path = tmp_path / "config.yaml"
    item = config.sources["receipts"]
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0.0",
                "min_age_seconds": 0,
                "state_dir": str(config.state_dir),
                "sources": {
                    "receipts": {
                        "input_dir": str(item.input_dir),
                        "output_dir": str(item.output_dir),
                        "after_success": "delete",
                    }
                },
            }
        )
    )
    assert main(["run", "--config", str(path), "--source", "receipts", "--dry-run"]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["needs_review"] == 1
    assert source.exists() and not config.state_dir.exists()
    assert main(["run", "--config", str(path), "--source", "unknown", "--dry-run"]) == 2


def test_source_suffix_and_recursive_paths(tmp_path):
    config, _, _ = queued(tmp_path)
    config = replace(
        config, sources={"receipts": replace(config.sources["receipts"], recursive=True)}
    )
    item = source_configs(config)[0]
    child = item.input_dir / "child"
    child.mkdir()
    (child / "CAPS.PDF").write_bytes(make_pdf())
    pairs = discover(item)
    assert (child / "CAPS.PDF", item.output_dir / "child/CAPS_ocr.PDF") in pairs


@pytest.mark.parametrize("failure", ["bad_pdf", "wrong_pages", "source_changed"])
def test_queue_validation_failure_keeps_original(tmp_path, failure):
    config, source, output = queued(tmp_path)
    provider = FakeProvider(
        output=b"invalid"
        if failure == "bad_pdf"
        else make_pdf("result", pages=2 if failure == "wrong_pages" else 1, hidden=True)
    )
    if failure == "source_changed":
        provider.callback = lambda: source.write_bytes(make_pdf("changed source"))
    result = only_result(config, provider=provider)
    assert result["status"] == "failed" and source.exists() and not output.exists()
    assert not list((config.state_dir / "handoffs").rglob("*.json"))


def test_existing_output_requires_explicit_overwrite_before_delete(tmp_path):
    config, source, output = queued(tmp_path)
    output.parent.mkdir()
    original_output = make_pdf("old output")
    output.write_bytes(original_output)
    assert only_result(config)["status"] == "failed"
    assert source.exists() and output.read_bytes() == original_output
    provider = FakeProvider()
    result = run_sources(config, Options(overwrite=True), provider_factory=lambda: provider)[
        "files"
    ][0]
    assert result["status"] == "replaced" and not source.exists()
    assert Path(result["backup"]).read_bytes() == original_output


def test_retry_flags_cannot_override_missing_handoff_output(tmp_path, monkeypatch):
    config, source, output = queued(tmp_path)
    original = block_delete(monkeypatch, source)
    only_result(config, provider=FakeProvider())
    monkeypatch.setattr(Path, "unlink", original)
    output.unlink()
    result = run_sources(
        config,
        Options(overwrite=True, redo_ocr=True, retry_uncertain=True),
        provider_factory=lambda: pytest.fail("no Azure"),
    )["files"][0]
    assert result["status"] == "needs_review" and source.exists() and not output.exists()


def test_changed_keep_policy_requires_recorded_output_before_delete(tmp_path):
    config, source, output = queued(tmp_path, after="keep")
    only_result(config, provider=FakeProvider())
    output_data = output.read_bytes()
    output.unlink()
    config = replace(
        config, sources={"receipts": replace(config.sources["receipts"], after_success="delete")}
    )
    assert only_result(config)["status"] == "needs_review" and source.exists()
    output.write_bytes(output_data)
    assert only_result(config)["source_action"] == "deleted" and not source.exists()


def test_selected_queue_leaves_other_input_untouched(tmp_path):
    config, source, output = queued(tmp_path, data=make_pdf("searchable"))
    second = replace(
        config.sources["receipts"],
        input_dir=tmp_path / "other-in",
        output_dir=tmp_path / "other-out",
    )
    second.input_dir.mkdir()
    untouched = second.input_dir / "other.pdf"
    untouched.write_bytes(make_pdf())
    config = replace(config, sources={**config.sources, "other": second})
    result = run_sources(
        config, Options(), "receipts", provider_factory=lambda: pytest.fail("no Azure")
    )
    assert result["counts"]["created"] == 1 and output.exists() and not source.exists()
    assert untouched.exists() and not second.output_dir.exists()


def test_source_lock_protects_different_destinations(tmp_path):
    import os

    from filelock import FileLock

    from pdf_ocr_pipeline.pipeline import process_file

    config, source, output = queued(tmp_path)
    item = source_configs(config)[0]
    lock_dir = config.state_dir / "locks"
    lock_dir.mkdir(parents=True)
    key = sha256(os.path.normcase(str(source.resolve())).encode())
    with (
        FileLock(lock_dir / f"source-{key}.lock", timeout=0),
        pytest.raises(OcrError, match="Another process"),
    ):
        process_file(source, output, item, Options())
    assert source.exists() and not output.exists()


def test_sources_use_independent_recursive_settings(tmp_path):
    roots = {}
    settings = {}
    for source_id, recursive in (("receipts", False), ("catalogs", True)):
        root = tmp_path / source_id
        (root / "nested").mkdir(parents=True)
        (root / "direct.pdf").write_bytes(make_pdf())
        (root / "nested/child.pdf").write_bytes(make_pdf())
        roots[source_id] = root
        settings[source_id] = {
            "input_dir": str(root),
            "output_dir": str(tmp_path / (source_id + "-out")),
            "recursive": recursive,
        }
    config = resolve_config(
        overrides={
            "sources": settings,
            "state_dir": str(tmp_path / "state"),
            "min_age_seconds": 0,
            "azure": {"endpoint": "https://example.com"},
        }
    ).config
    report = run_sources(
        config, Options(dry_run=True), provider_factory=lambda: pytest.fail("no Azure")
    )
    assert report["counts"]["planned"] == 3
    assert report["sources"]["receipts"]["planned"] == 1
    assert report["sources"]["catalogs"]["planned"] == 2
    assert any(
        Path(r["output"]) == tmp_path / "catalogs-out/nested/child.pdf" for r in report["files"]
    )
    assert not config.state_dir.exists()


def test_default_queue_runs_using_home_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    config = resolve_config(overrides={"min_age_seconds": 0}).config
    item = config.sources["default"]
    item.input_dir.mkdir(parents=True)
    source = item.input_dir / "searchable.pdf"
    original = make_pdf("native text")
    source.write_bytes(original)
    planned = run_sources(
        config, Options(dry_run=True), "default", provider_factory=lambda: pytest.fail("no Azure")
    )
    assert planned["counts"]["planned"] == 1
    assert not item.output_dir.exists() and not config.state_dir.exists()
    report = run_sources(
        config, Options(), "default", provider_factory=lambda: pytest.fail("no Azure")
    )
    assert report["sources"]["default"]["created"] == 1
    assert (item.output_dir / source.name).read_bytes() == original
    assert source.exists()
