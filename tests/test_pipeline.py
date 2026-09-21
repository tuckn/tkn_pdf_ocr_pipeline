import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeProvider, make_pdf
from filelock import FileLock

from pdf_ocr_pipeline.errors import OcrError
from pdf_ocr_pipeline.io_utils import sha256
from pdf_ocr_pipeline.pdf import inspect_pdf
from pdf_ocr_pipeline.pipeline import Options, discover, process_file, run


def input_file(config, data=None):
    config.input_dir.mkdir(exist_ok=True)
    path = config.input_dir / "sample.pdf"
    path.write_bytes(data or make_pdf())
    return path, config.output_dir / path.name


def test_end_to_end_and_idempotence(config):
    source, output = input_file(config)
    original = source.read_bytes()
    provider = FakeProvider()
    first = process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert first["status"] == "created"
    assert inspect_pdf(output.read_bytes()).text_pages == [1]
    assert source.read_bytes() == original
    second = process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert second["status"] == "unchanged"
    assert len(provider.submissions) == 1
    state = json.loads(Path(first["state"]).read_text())
    assert state["status"] == "completed"
    assert "synthetic-key" not in json.dumps(state)


def test_dry_run_is_readonly_no_provider(config):
    source, output = input_file(config)
    before = sorted(source.parent.parent.rglob("*"))
    result = process_file(
        source,
        output,
        config,
        Options(dry_run=True),
        provider_factory=lambda: pytest.fail("no network"),
    )
    assert result["status"] == "planned"
    assert sorted(source.parent.parent.rglob("*")) == before


def test_conflict_backup_and_no_source_overwrite(config):
    source, output = input_file(config)
    output.parent.mkdir()
    output.write_bytes(make_pdf("manually edited"))
    original = output.read_bytes()
    with pytest.raises(OcrError, match="Output exists"):
        process_file(source, output, config, Options())
    provider = FakeProvider()
    result = process_file(
        source, output, config, Options(overwrite=True), provider_factory=lambda: provider
    )
    assert result["status"] == "replaced"
    assert Path(result["backup"]).read_bytes() == original
    with pytest.raises(OcrError, match="different files"):
        process_file(source, source, config, Options(overwrite=True))


def test_resume_does_not_resubmit(config):
    source, output = input_file(config)
    provider = FakeProvider(fail_collect=True)
    with pytest.raises(OcrError):
        process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert not output.exists()
    provider.fail_collect = False
    process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert len(provider.submissions) == 1


def test_unknown_submission_requires_explicit_retry(config):
    source, output = input_file(config)
    provider = FakeProvider(fail_submit=True)
    with pytest.raises(OcrError):
        process_file(source, output, config, Options(), provider_factory=lambda: provider)
    provider.fail_submit = False
    with pytest.raises(OcrError, match="uncertain"):
        process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert len(provider.submissions) == 1
    process_file(
        source, output, config, Options(retry_uncertain=True), provider_factory=lambda: provider
    )
    assert len(provider.submissions) == 2


@pytest.mark.parametrize("data", [b"not pdf", make_pdf(pages=2), make_pdf()])
def test_invalid_or_incomplete_result_not_published(config, data):
    source, output = input_file(config)
    provider = FakeProvider(output=data)
    with pytest.raises(OcrError):
        process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert not output.exists()


@pytest.mark.parametrize("changed", ["source", "output"])
def test_detect_concurrent_changes(config, changed):
    source, output = input_file(config)

    def mutate():
        target = source if changed == "source" else output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"concurrent")

    provider = FakeProvider(callback=mutate)
    with pytest.raises(OcrError, match="changed"):
        process_file(source, output, config, Options(), provider_factory=lambda: provider)
    if changed == "output":
        assert output.read_bytes() == b"concurrent"
    else:
        assert not output.exists()


def test_atomic_publish_failure_resume(config, monkeypatch):
    import pdf_ocr_pipeline.pipeline as module

    source, output = input_file(config)
    provider = FakeProvider()
    real = module.atomic_write
    monkeypatch.setattr(module, "atomic_write", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert not output.exists()
    monkeypatch.setattr(module, "atomic_write", real)
    process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert len(provider.submissions) == 1


def test_ready_state_recovers_after_output_publication(config):
    source, output = input_file(config)
    provider = FakeProvider()
    result = process_file(source, output, config, Options(), provider_factory=lambda: provider)
    state_path = Path(result["state"])
    state = json.loads(state_path.read_text())
    state["status"] = "ready"
    state_path.write_text(json.dumps(state))
    assert process_file(source, output, config, Options())["status"] == "unchanged"


def test_recursive_discovery_case_and_overlap(config):
    source, _ = input_file(config)
    sub = source.parent / "child"
    sub.mkdir()
    (sub / "second.PDF").write_bytes(make_pdf())
    assert len(discover(config)) == 1
    pairs = discover(replace(config, recursive=True))
    assert len(pairs) == 2
    assert pairs[1][1].is_relative_to(config.output_dir)
    with pytest.raises(OcrError, match="non-nested"):
        discover(replace(config, output_dir=config.input_dir / "out"))


def test_age_limit_and_invalid_pdf_continue(config):
    source, output = input_file(config)
    assert (
        process_file(source, output, replace(config, min_age_seconds=60), Options())["reason"]
        == "input_not_stable_yet"
    )
    source.write_bytes(b"bad")
    report = run([(source, output)], config, Options())
    assert report["counts"]["failed"] == 1


def test_output_lock(config):
    source, output = input_file(config)
    key = sha256(os.path.normcase(str(output)).encode())
    lock = config.state_dir / "locks" / f"{key}.lock"
    lock.parent.mkdir(parents=True)
    with FileLock(lock, timeout=0), pytest.raises(OcrError, match="Another process"):
        process_file(source, output, config, Options())


def test_invalid_destination_rejected_in_dry_run(config):
    source, output = input_file(config)
    output.parent.write_text("a file, not a folder")
    with pytest.raises(OcrError, match="not a directory"):
        process_file(source, output, config, Options(dry_run=True))
    assert not config.state_dir.exists()


def test_run_report_and_dryrun_no_report(config):
    source, output = input_file(config)
    report = run([(source, output)], config, Options(dry_run=True))
    assert "run_report" not in report and not config.state_dir.exists()
    provider = FakeProvider()
    report = run([(source, output)], config, Options(), provider_factory=lambda: provider)
    assert json.loads(Path(report["run_report"]).read_text())["counts"]["created"] == 1


def test_encrypted_pdf_rejected(config):
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=240, height=320)
    writer.encrypt("password")
    data = BytesIO()
    writer.write(data)
    source, output = input_file(config, data.getvalue())
    with pytest.raises(OcrError, match="Encrypted"):
        process_file(source, output, config, Options(dry_run=True))
