from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from conftest import FakeProvider, make_pdf

import pdf_ocr_pipeline.cli as cli
from pdf_ocr_pipeline.config_output import config_lines
from pdf_ocr_pipeline.errors import OcrError
from pdf_ocr_pipeline.output import public_result
from pdf_ocr_pipeline.pipeline import Options, run


def checked(captured, machine):
    assert "\x1b" not in captured.out
    assert "[INFO]" not in captured.out and "[SUCCESS]" not in captured.out
    if machine:
        return json.loads(captured.out)
    lines = captured.out.splitlines()
    assert lines and all("=" in line for line in lines)
    return dict(line.split("=", 1) for line in lines)


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["convert", "input.pdf", "--output", "out.pdf"],
        ["verify", "input.pdf"],
        ["config", "list"],
        ["config", "init"],
        ["auth", "login"],
    ],
)
def test_every_command_uses_selected_format_without_affecting_dispatch(
    monkeypatch, capsys, args, machine
):
    result = {"status": "planned", "path": r"C:\Users\ExampleUser\資料\file = 1.pdf"}
    execute = Mock(return_value=result)
    monkeypatch.setattr(cli, "execute", execute)
    assert cli.main([*args, *(["--json"] if machine else [])]) == 0
    value = checked(capsys.readouterr(), machine)
    assert value == result
    assert execute.call_args.args[0].command == args[0]
    assert result == {"status": "planned", "path": r"C:\Users\ExampleUser\資料\file = 1.pdf"}


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_details_keep_failures_review_paths_and_counts_without_mutating_reports(
    monkeypatch, capsys, machine, full
):
    result = {
        "schema_version": "1.0.0",
        "dry_run": True,
        "counts": {"failed": 1, "needs_review": 1, "skipped": 2, "planned": 1},
        "files": [
            {"status": "skipped", "source": "a.pdf", "reason": "output_exists"},
            {"status": "skipped", "source": "b.pdf", "reason": "input_not_stable_yet"},
            {"status": "failed", "source": r"C:\long path\資料.pdf", "error": "invalid PDF"},
            {"status": "needs_review", "source": "review.pdf", "reason": "ocr_pages_without_text"},
            {"status": "planned", "source": "plan.pdf", "output": "out.pdf"},
        ],
    }
    before = copy.deepcopy(result)
    monkeypatch.setattr(cli, "execute", lambda args: result)
    flags = (["--json"] if machine else []) + (["--full-output"] if full else [])
    assert cli.main([*flags, "run"]) == 1
    capture = capsys.readouterr()
    value = checked(capture, machine)
    assert value["status"] == "failed"
    assert "[ERROR]" in capture.err and "[SUCCESS]" not in capture.err
    projected = public_result(result, full_output=full)
    assert result == before
    assert len(projected["files"]) == (5 if full else 3)
    assert projected["counts"] == result["counts"]
    if machine:
        assert value == projected
    else:
        assert value[f"files[{2 if full else 0}].source"] == r"C:\long path\資料.pdf"
        assert value[f"files[{2 if full else 0}].error"] == "invalid PDF"
    if not full:
        assert projected["omitted_files"] == 2
        assert projected["skipped_reasons"] == {"output_exists": 1, "input_not_stable_yet": 1}
        assert "--full-output" in projected["details_hint"]
    else:
        assert "omitted_files" not in projected


@pytest.mark.parametrize(
    "args", [["config", "list"], ["config", "init", "--dry-run"], ["auth", "login", "--dry-run"]]
)
@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_json_and_detail_flags_survive_nested_parsers(monkeypatch, capsys, args, position):
    execute = Mock(return_value={"status": "planned"})
    monkeypatch.setattr(cli, "execute", execute)
    args = list(args)
    args[position:position] = ["--json", "--full-output"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    parsed = execute.call_args.args[0]
    assert parsed.json and parsed.full_output


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["unknown"],
        ["convert"],
        ["config"],
        ["config", "unknown"],
        ["run", "--min-age-seconds", "oops"],
        ["run", "--auth-mode", "invalid"],
        ["run", "--unknown"],
        ["--quiet", "config", "list", "--verbose"],
        ["run", "--save-json", "--only-json"],
    ],
)
@pytest.mark.parametrize("machine", [False, True])
def test_argument_errors_keep_requested_format(args, capsys, machine):
    assert cli.main([*args, *(["--json"] if machine else [])]) == 2
    capture = capsys.readouterr()
    assert checked(capture, machine)["status"] == "failed"
    assert "[ERROR]" in capture.err and "Traceback" not in capture.err


@pytest.mark.parametrize(
    "args", [["--json=true", "run"], ["config", "--json", "invalid"], ["--json", "convert"]]
)
def test_json_parse_failures_are_a_single_valid_document(args, capsys):
    assert cli.main(args) == 2
    assert checked(capsys.readouterr(), True)["status"] == "failed"


@pytest.mark.parametrize(
    "exception,expected,status",
    [
        (OcrError("safe failure", path=r"C:\Users\ExampleUser\資料.pdf"), 2, "failed"),
        (
            PermissionError(13, "private implementation text", r"C:\Users\ExampleUser\資料.pdf"),
            2,
            "failed",
        ),
        (RuntimeError("secret-token-should-not-leak"), 1, "failed"),
        (KeyboardInterrupt(), 130, "interrupted"),
    ],
)
@pytest.mark.parametrize("machine", [False, True])
def test_runtime_errors_and_interruptions_keep_format_and_paths(
    monkeypatch, capsys, exception, expected, status, machine
):
    monkeypatch.setattr(cli, "execute", Mock(side_effect=exception))
    assert cli.main(["verify", "input.pdf", *(["--json"] if machine else [])]) == expected
    capture = capsys.readouterr()
    value = checked(capture, machine)
    assert value["status"] == status
    assert value["path"] == (
        getattr(exception, "path", None)
        or getattr(exception, "filename", None)
        or str(Path("input.pdf").absolute())
    )
    assert "secret-token-should-not-leak" not in capture.out + capture.err
    assert "private implementation text" not in capture.out + capture.err


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize(
    "contents",
    [
        "not: [valid",
        "[not, a, mapping]",
        'schema_version: "3.0.0"\nunknown: value\n',
        'schema_version: "9.0.0"\n',
    ],
)
def test_config_errors_expose_automatic_source_path(tmp_path, capsys, machine, contents):
    path = tmp_path / "home/config.yaml"
    path.parent.mkdir()
    path.write_text(contents, encoding="utf-8")
    assert cli.main(["config", "list", *(["--json"] if machine else [])]) == 2
    value = checked(capsys.readouterr(), machine)
    assert value["status"] == "failed" and value["path"] == str(path)


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize("verbosity", [[], ["--quiet"], ["--verbose"]])
def test_verbosity_only_changes_logs(monkeypatch, capsys, machine, verbosity):
    monkeypatch.setattr(cli, "execute", lambda args: {"status": "verified", "path": "input.pdf"})
    assert cli.main([*verbosity, "verify", "input.pdf", *(["--json"] if machine else [])]) == 0
    capture = capsys.readouterr()
    assert checked(capture, machine) == {"status": "verified", "path": "input.pdf"}
    assert ("[DEBUG]" in capture.err) == (verbosity == ["--verbose"])
    assert (capture.err == "") == (verbosity == ["--quiet"])


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize("artifact", [[], ["--save-json"], ["--only-json"]])
def test_convert_dry_run_no_writes_and_display_independent_of_artifact(
    config, tmp_path, monkeypatch, capsys, machine, artifact
):
    import pdf_ocr_pipeline.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "AzureRead", Mock(side_effect=AssertionError("no auth or network"))
    )
    source = tmp_path / "日本語 input.pdf"
    source.write_bytes(make_pdf())
    output = tmp_path / "result.pdf"
    args = [
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
        "--dry-run",
        *artifact,
    ]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert cli.main([*args, *(["--json"] if machine else [])]) == 0
    value = checked(capsys.readouterr(), machine)
    if machine:
        assert value["counts"]["planned"] == 1
        assert value["files"][0]["output"] == str(
            output.with_suffix(".json") if artifact == ["--only-json"] else output
        )
        assert ("json_output" in value["files"][0]) == (artifact == ["--save-json"])
    else:
        assert value["counts.planned"] == "1"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not output.exists() and not config.state_dir.exists()


@pytest.mark.parametrize("artifact", [[], ["--save-json"], ["--only-json"]])
@pytest.mark.parametrize("machine", [False, True])
def test_real_conversion_separates_stdout_from_ocr_artifacts(
    config, tmp_path, monkeypatch, capsys, artifact, machine
):
    import pdf_ocr_pipeline.pipeline as pipeline

    source = tmp_path / "input.pdf"
    source.write_bytes(make_pdf())
    output = tmp_path / "out.pdf"
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "AzureRead", lambda cfg: provider)
    monkeypatch.setenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "synthetic-key")
    assert (
        cli.main(
            [
                "convert",
                str(source),
                "--output",
                str(output),
                "--endpoint",
                config.azure.endpoint,
                "--auth-mode",
                "key",
                "--min-age-seconds",
                "0",
                "--state-dir",
                str(config.state_dir),
                *artifact,
                *(["--json"] if machine else []),
            ]
        )
        == 0
    )
    value = checked(capsys.readouterr(), machine)
    assert value["status"] == "success"
    assert output.exists() == (artifact != ["--only-json"])
    assert output.with_suffix(".json").exists() == bool(artifact)
    assert source.exists()
    reports = list((config.state_dir / "runs").glob("*.json"))
    saved = json.loads(reports[0].read_text(encoding="utf-8"))
    assert "status" not in saved and "omitted_files" not in saved
    assert saved["counts"]["created"] == 1
    assert provider.submissions


def test_summary_never_modifies_python_results_or_saved_reports(config, tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(make_pdf())
    output = tmp_path / "out.pdf"
    output.write_bytes(b"existing")
    result = run([(source, output), (source, output)], config, Options())
    before = copy.deepcopy(result)
    report = Path(result["run_report"])
    saved = report.read_bytes()
    brief = public_result(result)
    assert brief["files"] == [] and brief["counts"]["skipped"] == 2
    assert len(public_result(result, full_output=True)["files"]) == 2
    assert result == before and report.read_bytes() == saved


@pytest.mark.parametrize("machine", [False, True])
def test_real_verify_error_and_control_characters(tmp_path, capsys, machine):
    source = tmp_path / "資料.pdf"
    source.write_bytes(make_pdf("Native text"))
    assert cli.main(["verify", str(source), *(["--json"] if machine else [])]) == 0
    value = checked(capsys.readouterr(), machine)
    assert value["status"] == "verified" and value["path"] == str(source)
    assert (
        cli.main(["verify", str(source), "--expected-pages", "2", *(["--json"] if machine else [])])
        == 2
    )
    value = checked(capsys.readouterr(), machine)
    assert value["path"] == str(source) and value["status"] == "failed"
    lines = config_lines({"key\nname": "a\x1b[31m\r\n\t", "sizes": [(240.0, 320.0)]})
    assert lines == [r"key\nname=a\u001b[31m\r\n\t", "sizes[0][0]=240.0", "sizes[0][1]=320.0"]


def test_double_dash_keeps_json_named_path_positional(tmp_path, capsys):
    source = tmp_path / "--json"
    source.write_bytes(make_pdf())
    assert cli.main(["verify", "--", "--json"]) == 0
    assert checked(capsys.readouterr(), False)["path"] == str(source)


def test_json_flag_never_requests_an_ocr_artifact():
    base = ["convert", "input.pdf", "--output", "out.pdf"]
    regular = cli.parser().parse_args(base)
    machine = cli.parser().parse_args(["--json", *base])
    assert regular.save_json is machine.save_json is False
    saved = cli.parser().parse_args([*base, "--save-json", "--json"])
    assert saved.save_json is True and saved.json is True
    only = cli.parser().parse_args([*base, "--only-json", "--json"])
    assert only.only_json and only.json and not only.save_json


@pytest.mark.parametrize("machine", [False, True])
def test_json_destination_requires_artifact_flag(capsys, machine):
    assert (
        cli.main(
            [
                "convert",
                "input.pdf",
                "--output",
                "out.pdf",
                "--json-output",
                "ocr.json",
                *(["--json"] if machine else []),
            ]
        )
        == 2
    )
    value = checked(capsys.readouterr(), machine)
    assert "--save-json" in value["error"]


@pytest.mark.parametrize("machine", [False, True])
def test_tty_does_not_change_format(monkeypatch, machine):
    class Stream(io.StringIO):
        def isatty(self):
            return True

    output, errors = Stream(), Stream()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    monkeypatch.setattr(
        cli, "execute", lambda args: {"status": "verified", "path": r"C:\path\to\資料.pdf"}
    )
    assert cli.main(["verify", "input.pdf", *(["--json"] if machine else [])]) == 0
    rendered = output.getvalue()
    assert "\x1b" not in rendered
    assert (
        json.loads(rendered)["status"] == "verified"
        if machine
        else "status=verified" in rendered.splitlines()
    )


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize("redirected", [False, True])
def test_subprocess_pipe_and_redirection_preserve_format(tmp_path, machine, redirected):
    source = tmp_path / "長い資料 名.pdf"
    source.write_bytes(make_pdf("text"))
    environment = {
        **os.environ,
        "USERPROFILE": str(tmp_path / "isolated-home"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    executable = Path(sys.executable).with_name("tkn-pdf-ocr.exe")
    args = [str(executable), "verify", str(source), *(["--json"] if machine else [])]
    if redirected:
        target = tmp_path / "result.txt"
        with target.open("w", encoding="utf-8") as output:
            result = subprocess.run(
                args,
                cwd=tmp_path,
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        rendered = target.read_text(encoding="utf-8")
    else:
        result = subprocess.run(
            args,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        rendered = result.stdout
    assert result.returncode == 0, result.stderr
    assert "\x1b" not in rendered
    assert (
        json.loads(rendered)["path"] == str(source)
        if machine
        else f"path={source}" in rendered.splitlines()
    )


@pytest.mark.parametrize("machine", [False, True])
def test_real_config_init_and_login_dry_run(tmp_path, monkeypatch, capsys, machine):
    import pdf_ocr_pipeline.auth as auth

    target = tmp_path / "settings/config.yaml"
    flags = ["--json"] if machine else []
    assert cli.main([*flags, "config", "init", str(target), "--dry-run"]) == 0
    assert checked(capsys.readouterr(), machine)["status"] == "created"
    assert not target.parent.exists()
    assert cli.main(["config", "init", str(target), *flags]) == 0
    assert checked(capsys.readouterr(), machine)["path"] == str(target)
    before = target.read_bytes()
    assert cli.main([*flags, "config", "init", str(target)]) == 0
    assert checked(capsys.readouterr(), machine)["status"] == "unchanged"
    assert target.read_bytes() == before
    target.write_text(
        'schema_version: "3.0.0"\nazure:\n  endpoint: https://example.com\n', encoding="utf-8"
    )
    before = target.read_bytes()
    assert cli.main(["config", "init", str(target), *flags]) == 2
    value = checked(capsys.readouterr(), machine)
    assert value["status"] == "failed" and value["path"] == str(target)
    assert target.read_bytes() == before
    monkeypatch.setattr(
        auth, "InteractiveBrowserCredential", Mock(side_effect=AssertionError("no auth"))
    )
    assert cli.main(["auth", "login", "--config", str(target), "--dry-run", *flags]) == 0
    value = checked(capsys.readouterr(), machine)
    assert value["status"] == "planned"
    assert target.read_bytes() == before
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("machine", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_real_queue_dry_run_partial_failure_skip_summary_and_no_mutations(
    tmp_path, monkeypatch, capsys, machine, full
):
    import pdf_ocr_pipeline.pipeline as pipeline

    monkeypatch.setattr(pipeline, "AzureRead", Mock(side_effect=AssertionError("no network")))
    incoming, output = tmp_path / "incoming", tmp_path / "output"
    incoming.mkdir()
    output.mkdir()
    for name in ("a.pdf", "b.pdf", "planned.pdf"):
        (incoming / name).write_bytes(make_pdf())
    for name in ("a.pdf", "b.pdf"):
        (output / name).write_bytes(b"existing output")
    (incoming / "broken.pdf").write_bytes(b"invalid PDF")
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0.0",
                "min_age_seconds": 0,
                "state_dir": str(tmp_path / "state"),
                "azure": {"endpoint": "https://example.com"},
                "sources": {
                    "receipts": {
                        "input_dir": str(incoming),
                        "output_dir": str(output),
                        "after_success": "delete",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    tree = sorted(tmp_path.rglob("*"))
    args = ["run", "--source", "receipts", "--config", str(config_path), "--dry-run"]
    assert (
        cli.main([*args, *(["--json"] if machine else []), *(["--full-output"] if full else [])])
        == 1
    )
    value = checked(capsys.readouterr(), machine)
    assert value["status"] == "failed"
    if machine:
        assert value["counts"] == {
            "created": 0,
            "replaced": 0,
            "skipped": 2,
            "planned": 1,
            "failed": 1,
            "needs_review": 0,
        }
        assert len(value["files"]) == (4 if full else 2)
        failure = next(item for item in value["files"] if item["status"] == "failed")
        assert failure["source"] == str(incoming / "broken.pdf") and failure["error"]
    else:
        assert value["counts.failed"] == "1" and value["counts.planned"] == "1"
        assert value["counts.skipped"] == "2"
    assert sorted(tmp_path.rglob("*")) == tree
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("machine", [False, True])
def test_long_windows_path_is_copyable_and_not_truncated(monkeypatch, capsys, machine):
    path = "C:\\" + "copyable folder\\" * 40 + "資料=1.pdf"
    monkeypatch.setattr(cli, "execute", lambda args: {"status": "verified", "path": path})
    assert cli.main(["verify", "input.pdf", *(["--json"] if machine else [])]) == 0
    assert checked(capsys.readouterr(), machine)["path"] == path


@pytest.mark.parametrize(
    "args", [["--help"], ["config", "--help"], ["run", "--help"], ["--version"]]
)
def test_json_does_not_replace_help_or_version(args, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--json", *args])
    assert caught.value.code == 0
    result = capsys.readouterr()
    assert not result.err
    if args == ["--version"]:
        assert result.out.strip() == "0.8.0"
    else:
        assert result.out.startswith("usage:")
