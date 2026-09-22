import json
from dataclasses import replace

import httpx
import pytest
from conftest import FakeProvider, make_pdf

from pdf_ocr_pipeline.azure import AzureRead
from pdf_ocr_pipeline.cli import main
from pdf_ocr_pipeline.errors import OcrError

OP = "https://example.cognitiveservices.azure.com/documentintelligence/documentModels/prebuilt-read/analyzeResults/abc?api-version=2024-11-30"


def test_real_adapter_transport_contract(config):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Ocp-Apim-Subscription-Key"] == "synthetic-key"
        if request.method == "POST":
            assert request.url.params["output"] == "pdf"
            assert request.url.params["api-version"] == "2024-11-30"
            assert request.headers["Content-Type"] == "application/pdf"
            return httpx.Response(202, headers={"Operation-Location": OP})
        if request.url.path.endswith("/pdf"):
            return httpx.Response(
                200, headers={"Content-Type": "application/pdf"}, content=make_pdf("recognized")
            )
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "analyzeResult": {
                    "modelId": "prebuilt-read",
                    "pages": [{"pageNumber": 1, "words": [{"content": "x"}]}],
                },
            },
        )

    provider = AzureRead(config.azure, transport=httpx.MockTransport(handle))
    try:
        provider.prepare()
        operation = provider.submit(make_pdf())
        data, pages = provider.collect(operation)
        assert pages == [1] and data.startswith(b"%PDF")
        assert [r.method for r in requests] == ["POST", "GET", "GET"]
    finally:
        provider.close()


@pytest.mark.parametrize(
    "url",
    [
        OP.replace("example.cognitiveservices.azure.com", "evil.example"),
        OP.replace("https://", "http://"),
        OP + "&secret=x",
        OP.replace("prebuilt-read", "prebuilt-layout"),
    ],
)
def test_operation_url_never_leaks_auth(config, url):
    provider = AzureRead(
        config.azure,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("must not request untrusted URL")
        ),
    )
    try:
        with pytest.raises(OcrError, match="untrusted"):
            provider.collect(url)
    finally:
        provider.close()


def test_post_never_retried_and_response_redacted(config):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, text="private OCR content synthetic-key")

    provider = AzureRead(config.azure, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(OcrError) as caught:
            provider.submit(make_pdf())
        assert len(calls) == 1
        assert "synthetic-key" not in str(caught.value)
        assert "private OCR" not in str(caught.value)
    finally:
        provider.close()


def test_get_retry_is_bounded(config, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"})

    provider = AzureRead(
        replace(config.azure, max_get_retries=2), transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(provider, "_wait", lambda *args: None)
    try:
        with pytest.raises(OcrError, match="bounded"):
            provider.collect(OP)
        assert len(calls) == 3
    finally:
        provider.close()


def test_cli_end_to_end(config, monkeypatch, capsys, tmp_path):
    import pdf_ocr_pipeline.pipeline as module

    source = tmp_path / "日本語 入力.pdf"
    output = tmp_path / "output.pdf"
    source.write_bytes(make_pdf())
    provider = FakeProvider()
    monkeypatch.setattr(module, "AzureRead", lambda cfg: provider)
    result = main(
        [
            "convert",
            str(source),
            "--output",
            str(output),
            "--min-age-seconds",
            "0",
            "--state-dir",
            str(config.state_dir),
            "--endpoint",
            config.azure.endpoint,
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out)["counts"]["created"] == 1
    assert "[SUCCESS]" in captured.err and "\x1b[" not in captured.err
    assert main(["verify", str(output), "--expected-pages", "1", "--require-text"]) == 0


def test_cli_errors_are_json(capsys):
    assert main(["convert"]) == 2
    result = capsys.readouterr()
    assert json.loads(result.out)["status"] == "failed"
    assert "[ERROR]" in result.err


def test_config_show_and_logging_modes(capsys):
    assert main(["config", "show", "--quiet"]) == 0
    result = capsys.readouterr()
    assert not result.err
    assert json.loads(result.out)["effective_schema_version"] == "3.0.0"
    assert main(["--verbose", "config", "show"]) == 0
    assert "[DEBUG]" in capsys.readouterr().err
    assert main(["--quiet", "config", "show", "--verbose"]) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["convert", "--help"],
        ["run", "--help"],
        ["config", "init", "--help"],
        ["verify", "--help"],
        ["--version"],
    ],
)
def test_help(argv):
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--input-dir", "incoming"],
        ["run", "--output-dir", "outgoing"],
        ["convert", "input.pdf", "--output-dir", "outgoing"],
        ["convert", "input.pdf"],
    ],
)
def test_removed_folder_options_and_missing_output_rejected(argv, capsys):
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


@pytest.mark.parametrize("option", ["--recursive", "--no-recursive"])
def test_recursive_is_configured_per_source_not_by_cli(option, capsys):
    assert main(["run", option]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
