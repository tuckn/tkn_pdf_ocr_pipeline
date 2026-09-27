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
    assert result["status"] == "created" and result["source_action"] == "deleted"
    assert not source.exists()
    assert inspect_pdf(output.read_bytes()).image_hashes == inspect_pdf(original).image_hashes
    source.write_bytes(original)
    again = only_result(config)
    assert again["status"] == "skipped" and again["reason"] == "output_exists"
    assert source.exists()


def test_source_rename_treats_existing_output_as_collision(tmp_path):
    config, source, output = queued(tmp_path, after="keep")
    assert only_result(config, provider=FakeProvider())["status"] == "created"
    config = replace(config, sources={"cards": config.sources["receipts"]})
    again = only_result(config)
    assert again["status"] == "skipped" and source.exists() and output.exists()
    output.unlink()
    fresh = only_result(config, provider=FakeProvider())
    assert fresh["status"] == "created" and fresh["source_id"] == "cards"


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


def test_failed_delete_requires_manual_cleanup(tmp_path, monkeypatch):
    config, source, output = queued(tmp_path)
    real_unlink = Path.unlink

    def fail_source_unlink(path, *args, **kwargs):
        if path == source.resolve():
            raise PermissionError("synthetic sharing violation")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)
    first = only_result(config, provider=FakeProvider())
    assert first["status"] == "needs_review" and first["reason"] == "input_delete_failed"
    assert source.exists() and output.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    second = only_result(config)
    assert second["status"] == "skipped" and source.exists()


def test_input_changed_after_output_publication_is_not_deleted(tmp_path, monkeypatch):
    import pdf_ocr_pipeline.pipeline as module

    config, source, output = queued(tmp_path)
    original_write = module.atomic_write
    changed = make_pdf("replacement input")

    def publish_and_change(path, data, **kwargs):
        original_write(path, data, **kwargs)
        if path == output.resolve():
            source.write_bytes(changed)

    monkeypatch.setattr(module, "atomic_write", publish_and_change)
    result = only_result(config, provider=FakeProvider())
    assert result["status"] == "needs_review"
    assert result["reason"] == "input_changed_before_delete"
    assert source.read_bytes() == changed and output.exists()


def test_no_recognized_words_requires_review(tmp_path):
    class NoWords(FakeProvider):
        def collect(self, operation_url, *, include_pdf=True):
            return (
                make_pdf(),
                [],
                {"modelId": "prebuilt-read", "pages": [{"pageNumber": 1, "lines": []}]},
            )

    config, source, output = queued(tmp_path)
    first = only_result(config, provider=NoWords())
    assert first["reason"] == "ocr_pages_without_text"
    assert first["status"] == "needs_review" and source.exists() and not output.exists()


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


def test_existing_output_skips_unless_overwrite_is_explicit(tmp_path):
    config, source, output = queued(tmp_path)
    output.parent.mkdir()
    original_output = make_pdf("old output")
    output.write_bytes(original_output)
    skipped = only_result(config)
    assert skipped["status"] == "skipped" and skipped["reason"] == "output_exists"
    assert source.exists() and output.read_bytes() == original_output
    provider = FakeProvider()
    result = run_sources(config, Options(overwrite=True), provider_factory=lambda: provider)[
        "files"
    ][0]
    assert result["status"] == "replaced" and not source.exists()
    assert Path(result["backup"]).read_bytes() == original_output


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
