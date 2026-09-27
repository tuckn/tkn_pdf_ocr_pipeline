import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeProvider, make_pdf

from pdf_ocr_pipeline.config import resolve_config
from pdf_ocr_pipeline.errors import OcrError
from pdf_ocr_pipeline.pipeline import Options, process_file, run_sources


def test_pdf_and_json_share_one_azure_analysis(config, tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(make_pdf())
    output = tmp_path / "searchable.pdf"
    json_output = tmp_path / "ocr" / "searchable.json"
    provider = FakeProvider()
    options = Options(json=True, json_output=json_output)
    first = process_file(source, output, config, options, provider_factory=lambda: provider)
    assert first["status"] == "created"
    assert output.exists() and json_output.exists()
    value = json.loads(json_output.read_text(encoding="utf-8"))
    prediction = value["TextRecognition"]["responsev2"]["predictionOutput"]
    assert prediction["fullText"] == "Receipt 123"
    assert prediction["results"][0]["lines"][0]["text"] == "Receipt 123"
    assert value["sourceFileName"] == "input.pdf"
    assert value["analyzed_pages"] == [1]
    assert value["azureAnalyzeResult"]["modelId"] == "prebuilt-read"
    assert len(provider.submissions) == 1
    assert provider.pdf_requests == [True]
    again = process_file(source, output, config, options, provider_factory=lambda: provider)
    assert again["status"] == "unchanged" and len(provider.submissions) == 1


def test_json_only_analyzes_searchable_pdf_without_writing_pdf(config, tmp_path):
    source = tmp_path / "searchable.pdf"
    source.write_bytes(make_pdf("already searchable"))
    placeholder = tmp_path / "unused.pdf"
    json_output = tmp_path / "ocr.json"
    provider = FakeProvider()
    result = process_file(
        source,
        placeholder,
        config,
        Options(only_json=True, json_output=json_output),
        provider_factory=lambda: provider,
    )
    assert result["status"] == "created" and result["output"] == str(json_output)
    assert json_output.exists() and not placeholder.exists()
    assert json.loads(json_output.read_text(encoding="utf-8"))["source_pages"] == 1
    assert provider.collections == 1
    assert provider.pdf_requests == [False]


def test_json_only_dry_run_writes_nothing(config, tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(make_pdf())
    output = tmp_path / "ocr.json"
    result = process_file(
        source,
        tmp_path / "unused.pdf",
        config,
        Options(only_json=True, json_output=output, dry_run=True),
        provider_factory=lambda: (_ for _ in ()).throw(AssertionError("network")),
    )
    assert result["status"] == "planned"
    assert not output.exists() and not config.state_dir.exists()


def test_queue_json_destination_and_verified_cleanup(tmp_path):
    resolved = resolve_config(
        overrides={
            "state_dir": str(tmp_path / "state"),
            "min_age_seconds": 0,
            "sources": {
                "receipts": {
                    "input_dir": str(tmp_path / "in"),
                    "output_dir": str(tmp_path / "pdf"),
                    "json_output_dir": str(tmp_path / "json"),
                    "after_success": "delete",
                    "output_suffix": "_ocr",
                }
            },
            "azure": {
                "endpoint": "https://example.cognitiveservices.azure.com",
                "auth_mode": "key",
                "key_env": "TEST_OCR_KEY",
            },
        }
    )
    config = resolved.config
    assert config.sources["receipts"].json_output_dir == tmp_path / "json"
    source = tmp_path / "in" / "sample.pdf"
    source.parent.mkdir()
    source.write_bytes(make_pdf())
    provider = FakeProvider()
    report = run_sources(config, Options(json=True), provider_factory=lambda: provider)
    item = report["files"][0]
    assert item["status"] == "created" and item["source_action"] == "deleted"
    assert (tmp_path / "pdf" / "sample_ocr.pdf").exists()
    assert (tmp_path / "json" / "sample_ocr.json").exists()
    assert not source.exists()
    handoff = json.loads(Path(item["handoff"]).read_text(encoding="utf-8"))
    assert handoff["json_output"] == str(tmp_path / "json" / "sample_ocr.json")
    assert handoff["json_sha256"] == item["json_sha256"]


def test_cli_only_json_without_pdf_output(config, tmp_path, monkeypatch, capsys):
    import pdf_ocr_pipeline.pipeline as module
    from pdf_ocr_pipeline.cli import main

    source = tmp_path / "input.pdf"
    source.write_bytes(make_pdf())
    json_output = tmp_path / "extracted.json"
    provider = FakeProvider()
    monkeypatch.setenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "synthetic-key")
    monkeypatch.setattr(module, "AzureRead", lambda _config: provider)
    status = main(
        [
            "convert",
            str(source),
            "--only-json",
            "--json-output",
            str(json_output),
            "--min-age-seconds",
            "0",
            "--state-dir",
            str(config.state_dir),
            "--endpoint",
            config.azure.endpoint,
            "--auth-mode",
            "key",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert status == 0 and report["files"][0]["output"] == str(json_output)
    assert json_output.exists() and not json_output.with_suffix(".pdf").exists()


def test_json_publication_failure_keeps_input_and_requires_review(tmp_path, monkeypatch):
    import pdf_ocr_pipeline.pipeline as module

    config = resolve_config(
        overrides={
            "state_dir": str(tmp_path / "state"),
            "min_age_seconds": 0,
            "sources": {
                "receipts": {
                    "input_dir": str(tmp_path / "in"),
                    "output_dir": str(tmp_path / "out"),
                    "after_success": "delete",
                }
            },
            "azure": {
                "endpoint": "https://example.cognitiveservices.azure.com",
                "auth_mode": "key",
                "key_env": "TEST_OCR_KEY",
            },
        }
    ).config
    source = tmp_path / "in" / "sample.pdf"
    source.parent.mkdir()
    source.write_bytes(make_pdf())
    original = module.atomic_write

    def fail_json(path, data, **kwargs):
        if path.suffix == ".json":
            raise OSError("synthetic JSON write failure")
        return original(path, data, **kwargs)

    monkeypatch.setattr(module, "atomic_write", fail_json)
    first = run_sources(config, Options(json=True), provider_factory=FakeProvider)
    assert first["files"][0]["status"] == "failed" and source.exists()
    assert not (tmp_path / "out" / "sample.pdf").exists()
    monkeypatch.setattr(module, "atomic_write", original)
    second = run_sources(config, Options(json=True), provider_factory=FakeProvider)
    assert second["files"][0]["status"] == "needs_review" and source.exists()


def test_json_opt_in_and_conflict_without_azure(config, tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(make_pdf())
    output = tmp_path / "searchable.pdf"
    json_output = tmp_path / "searchable.json"
    provider = FakeProvider()
    process_file(source, output, config, Options(), provider_factory=lambda: provider)
    assert not json_output.exists()
    json_output.write_text('{"edited":true}', encoding="utf-8")
    with pytest.raises(OcrError, match="JSON output exists"):
        process_file(
            source,
            tmp_path / "different.pdf",
            config,
            Options(json=True, json_output=json_output),
            provider_factory=lambda: (_ for _ in ()).throw(AssertionError("network")),
        )
    assert json_output.read_text(encoding="utf-8") == '{"edited":true}'


def test_json_folder_may_be_inside_own_pdf_folder(tmp_path):
    from pdf_ocr_pipeline.pipeline import source_configs

    config = resolve_config(
        overrides={
            "state_dir": str(tmp_path / "state"),
            "sources": {
                "receipts": {
                    "input_dir": str(tmp_path / "in"),
                    "output_dir": str(tmp_path / "out"),
                    "json_output_dir": str(tmp_path / "out" / "json"),
                }
            },
        }
    ).config
    assert len(source_configs(config)) == 1


def test_json_only_named_queue_deletes_after_verified_json(tmp_path):
    config = resolve_config(
        overrides={
            "state_dir": str(tmp_path / "state"),
            "min_age_seconds": 0,
            "sources": {
                "receipts": {
                    "input_dir": str(tmp_path / "in"),
                    "output_dir": str(tmp_path / "out"),
                    "json_output_dir": str(tmp_path / "json"),
                    "after_success": "delete",
                }
            },
            "azure": {
                "endpoint": "https://example.cognitiveservices.azure.com",
                "auth_mode": "key",
                "key_env": "TEST_OCR_KEY",
            },
        }
    ).config
    source = tmp_path / "in" / "sample.pdf"
    source.parent.mkdir()
    source.write_bytes(make_pdf())
    report = run_sources(config, Options(only_json=True), provider_factory=FakeProvider)
    item = report["files"][0]
    assert item["status"] == "created" and item["source_action"] == "deleted"
    assert not source.exists()
    assert (tmp_path / "json" / "sample.json").exists()
    assert not (tmp_path / "out" / "sample.pdf").exists()

    class NoWords(FakeProvider):
        def collect(self, operation_url, *, include_pdf=True):
            return (
                None,
                [],
                {
                    "modelId": "prebuilt-read",
                    "pages": [{"pageNumber": 1, "lines": [], "words": []}],
                },
            )

    unreadable = tmp_path / "in" / "unreadable.pdf"
    unreadable.write_bytes(make_pdf())
    review = run_sources(config, Options(only_json=True), provider_factory=NoWords)
    assert review["files"][0]["status"] == "needs_review"
    assert unreadable.exists() and not (tmp_path / "json" / "unreadable.json").exists()


def test_queue_refuses_later_delete_when_json_is_missing(tmp_path):
    config = resolve_config(
        overrides={
            "state_dir": str(tmp_path / "state"),
            "min_age_seconds": 0,
            "sources": {
                "receipts": {
                    "input_dir": str(tmp_path / "in"),
                    "output_dir": str(tmp_path / "out"),
                    "after_success": "keep",
                }
            },
            "azure": {
                "endpoint": "https://example.cognitiveservices.azure.com",
                "auth_mode": "key",
                "key_env": "TEST_OCR_KEY",
            },
        }
    ).config
    source = tmp_path / "in" / "sample.pdf"
    source.parent.mkdir()
    source.write_bytes(make_pdf())
    first = run_sources(config, Options(json=True), provider_factory=FakeProvider)
    assert first["files"][0]["source_action"] == "kept"
    (tmp_path / "out" / "sample.json").unlink()
    source_config = replace(config.sources["receipts"], after_success="delete")
    changed = replace(config, sources={"receipts": source_config})
    later = run_sources(changed, Options(json=True), provider_factory=FakeProvider)
    assert later["files"][0]["status"] == "needs_review"
    assert source.exists()
