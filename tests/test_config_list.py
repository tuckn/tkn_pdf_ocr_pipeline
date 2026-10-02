import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pdf_ocr_pipeline.cli import main
from pdf_ocr_pipeline.config_output import config_lines


def test_copyable_entries_and_single_line_control_escapes():
    lines = config_lines(
        {
            "path": r"C:\Users\ExampleUser\日本語 profiles",
            "items": [{"enabled": True}, False, None, 12, 1.5],
            "empty_list": [],
            "empty_map": {},
            "text": 'first\r\nsecond\t"quoted"=value\b\f\x00\x1b\x7f\x85\u2028\u2029',
        }
    )
    assert lines == [
        r"path=C:\Users\ExampleUser\日本語 profiles",
        "items[0].enabled=true",
        "items[1]=false",
        "items[2]=null",
        "items[3]=12",
        "items[4]=1.5",
        "empty_list=[]",
        "empty_map={}",
        r'text=first\r\nsecond\t"quoted"=value\b\f\u0000\u001b\u007f\u0085\u2028\u2029',
    ]
    assert len("\n".join(lines).splitlines()) == len(lines)


@pytest.mark.parametrize("machine", [False, True])
def test_config_list_layers_metadata_secrets_and_readonly(tmp_path, monkeypatch, capsys, machine):
    import pdf_ocr_pipeline.auth as auth
    import pdf_ocr_pipeline.cli as cli

    # Resolve all home paths in an isolated tree, including built-in queue paths.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "user_config_path", lambda: home / "config.yaml")
    monkeypatch.setattr(
        auth, "InteractiveBrowserCredential", lambda *a, **kw: pytest.fail("must not authenticate")
    )
    monkeypatch.setenv("TEST_OCR_KEY", "secret-must-not-appear")
    settings = [
        (home / "config.yaml", {"min_age_seconds": 10}),
        (tmp_path / ".tkn/config.yaml", {"max_pages": 500}),
        (
            tmp_path / "explicit.yaml",
            {"max_pages": 200, "azure": {"auth_mode": "key", "key_env": "TEST_OCR_KEY"}},
        ),
    ]
    for path, values in settings:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"schema_version": "3.0.1", **values}), encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    tree = sorted(tmp_path.rglob("*"))
    args = ["config", "list", "--config", str(settings[-1][0])]
    assert main(args + (["--json"] if machine else [])) == 0
    captured = capsys.readouterr()
    assert captured.err == "[INFO] Showing resolved configuration\n"
    assert "secret-must-not-appear" not in captured.out + captured.err
    if machine:
        result = json.loads(captured.out)
        assert result["effective_schema_version"] == "3.0.0"
        assert result["settings"]["min_age_seconds"] == 10
        assert result["settings"]["max_pages"] == 200
        assert result["settings"]["azure"]["endpoint"] is None
        assert result["settings"]["sources"]["default"]["recursive"] is False
        assert result["sources"] == [
            {"path": str(p.resolve()), "schema_version": "3.0.1", "migration": False}
            for p, _ in settings
        ]
        assert result["winning_sources"]["max_pages"] == str(settings[-1][0].resolve())
        assert result["has_in_memory_migrations"] is False
    else:
        lines = captured.out.splitlines()
        assert "effective_schema_version=3.0.0" in lines
        assert "settings.min_age_seconds=10" in lines
        assert "settings.max_pages=200" in lines
        assert "settings.azure.endpoint=null" in lines
        assert "settings.sources.default.recursive=false" in lines
        assert "settings.sources.default.output_suffix=" in lines
        for index, (path, _) in enumerate(settings):
            assert f"sources[{index}].path={path.resolve()}" in lines
            assert f"sources[{index}].schema_version=3.0.1" in lines
            assert f"sources[{index}].migration=false" in lines
        assert f"winning_sources.max_pages={settings[-1][0].resolve()}" in lines
        assert "has_in_memory_migrations=false" in lines
    assert sorted(tmp_path.rglob("*")) == tree
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("machine", [False, True])
def test_listing_without_config_creates_nothing(tmp_path, capsys, machine):
    assert main(["config", "list"] + (["--json"] if machine else [])) == 0
    captured = capsys.readouterr()
    if machine:
        assert json.loads(captured.out)["sources"] == []
    else:
        assert "sources=[]" in captured.out.splitlines()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("machine", [False, True])
def test_config_list_quiet_retains_output(capsys, machine):
    assert main(["config", "list", "--quiet"] + (["--json"] if machine else [])) == 0
    result = capsys.readouterr()
    assert result.err == "" and result.out
    if machine:
        assert json.loads(result.out)["effective_schema_version"] == "3.0.0"


def test_config_help_and_removed_show(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["config", "--help"])
    assert caught.value.code == 0
    help_output = capsys.readouterr().out
    assert "{list,init}" in help_output and "{show" not in help_output
    with pytest.raises(SystemExit) as caught:
        main(["config", "list", "--help"])
    assert caught.value.code == 0
    assert "--json" in capsys.readouterr().out
    assert main(["config", "show"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


@pytest.mark.parametrize("machine", [False, True])
def test_listing_invalid_config_keeps_json_error(tmp_path, capsys, machine):
    assert (
        main(
            ["config", "list", "--config", str(tmp_path / "missing.yaml")]
            + (["--json"] if machine else [])
        )
        == 2
    )
    result = capsys.readouterr()
    assert json.loads(result.out)["status"] == "failed"
    assert "[ERROR]" in result.err and "[SUCCESS]" not in result.err


@pytest.mark.parametrize("machine", [False, True])
def test_installed_entrypoint_outside_repository(tmp_path, machine):
    environment = {
        **os.environ,
        "USERPROFILE": str(tmp_path / "home"),
        "HOME": str(tmp_path / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    executable = Path(sys.executable).with_name("tkn-pdf-ocr.exe")
    result = subprocess.run(
        [str(executable), "config", "list"] + (["--json"] if machine else []),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == "[INFO] Showing resolved configuration\n"
    if machine:
        assert json.loads(result.stdout)["effective_schema_version"] == "3.0.0"
    else:
        assert "effective_schema_version=3.0.0" in result.stdout.splitlines()
    assert list(tmp_path.iterdir()) == []
