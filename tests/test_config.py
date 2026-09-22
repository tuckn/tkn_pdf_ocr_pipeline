from pathlib import Path

import pytest
import yaml

from pdf_ocr_pipeline.config import example_bytes, init_config, resolve_config
from pdf_ocr_pipeline.errors import OcrError


def put(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": "3.0.0", **values}), encoding="utf-8")


def test_five_layer_precedence(tmp_path):
    put(tmp_path / "home/config.yaml", {"max_file_mb": 100, "min_age_seconds": 10})
    put(
        tmp_path / ".tkn/config.yaml",
        {"max_file_mb": 200, "sources": {"receipts": {"recursive": True}}},
    )
    put(tmp_path / "explicit.yaml", {"max_file_mb": 250})
    resolved = resolve_config(tmp_path / "explicit.yaml", {"max_file_mb": 300})
    assert resolved.config.max_file_mb == 300
    assert resolved.config.min_age_seconds == 10
    assert resolved.config.sources["receipts"].recursive is True
    assert resolved.config.max_pages == 2000
    assert resolved.winning_sources["max_file_mb"] == "CLI"
    assert len(resolved.sources) == 3
    assert "schema_version" not in resolved.values
    assert resolved.config.azure.endpoint is None


@pytest.mark.parametrize(
    "version", [None, 1, "1", "02.0.0", "0.1.0", "1.0.0", "2.0.0", "2.1.0", "3.1.0", "3.0.0-rc1"]
)
def test_reject_versions(tmp_path, version):
    path = tmp_path / "bad.yaml"
    put(path, {"schema_version": version})
    with pytest.raises(OcrError, match="schema_version"):
        resolve_config(path)


@pytest.mark.parametrize("version", ["3.0.0", "3.0.99"])
def test_patch_compatible(tmp_path, version):
    put(tmp_path / "ok.yaml", {"schema_version": version})
    resolved = resolve_config(tmp_path / "ok.yaml")
    assert resolved.sources[0]["schema_version"] == version
    assert not resolved.sources[0]["migration"]


@pytest.mark.parametrize(
    "bad",
    [
        {"unknown_key": 1},
        {"azure": {"bad_key": True}},
        {"sources": {"receipts": {"recursive": "yes"}}},
        {"max_file_mb": True},
        {"existing_text": "ignore"},
        {"max_file_mb": 0},
        {"azure": {"api_version": "2023-07-31"}},
        {"azure": {"endpoint": "http://example.com"}},
        {"azure": {"endpoint": "https://user:secret@example.com"}},
        {"azure": {"request_timeout_seconds": 0}},
        {"state_dir": None},
        {"azure": {"key_env": "key-value-that-is-not-an-env-name"}},
    ],
)
def test_each_source_validated_before_merge(tmp_path, bad):
    put(tmp_path / "home/config.yaml", bad)
    put(tmp_path / "explicit.yaml", {"max_file_mb": 300})
    with pytest.raises(OcrError):
        resolve_config(tmp_path / "explicit.yaml")


def test_init_protection_and_backup(tmp_path):
    path = tmp_path / "config.yaml"
    assert init_config(path, dry_run=True)["status"] == "created"
    assert not path.exists()
    assert init_config(path)["status"] == "created"
    assert path.read_text().startswith('schema_version: "3.0.0"')
    assert init_config(path)["status"] == "unchanged"
    path.write_text("edited", encoding="utf-8")
    with pytest.raises(OcrError):
        init_config(path)
    result = init_config(path, force=True)
    assert Path(result["backup"]).read_text() == "edited"
    assert path.read_bytes() == example_bytes()


def test_readonly_show_and_missing_explicit(tmp_path):
    before = list(tmp_path.rglob("*"))
    resolved = resolve_config(
        overrides={"sources": {"receipts": {"output_dir": "relative folder"}}}
    )
    assert resolved.config.sources["receipts"].output_dir == tmp_path / "relative folder"
    assert list(tmp_path.rglob("*")) == before
    with pytest.raises(OcrError, match="does not exist"):
        resolve_config(tmp_path / "missing.yaml")


def test_home_and_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = resolve_config(
        overrides={
            "sources": {"receipts": {"output_dir": "~/outputs", "input_dir": str(tmp_path / "in")}}
        }
    )
    assert resolved.config.sources["receipts"].output_dir == Path.home() / "outputs"
    assert resolved.config.sources["receipts"].input_dir == tmp_path / "in"


@pytest.mark.parametrize("key", ["input_dir", "output_dir"])
@pytest.mark.parametrize("value", [None, "C:/path/to/pdf"])
def test_top_level_folder_settings_are_rejected(tmp_path, key, value):
    path = tmp_path / "old.yaml"
    put(path, {key: value})
    with pytest.raises(OcrError, match="Unknown config key"):
        resolve_config(path)


def test_default_settings_only_expose_sources():
    resolved = resolve_config()
    assert set(resolved.values["sources"]) == {"default"}
    source = resolved.config.sources["default"]
    assert source.input_dir == (Path.home() / ".tkn/pdf_ocr_pipeline/data/incoming").resolve()
    assert source.output_dir == (Path.home() / ".tkn/pdf_ocr_pipeline/data/searchable").resolve()
    assert "input_dir" not in resolved.values and "output_dir" not in resolved.values


@pytest.mark.parametrize("value", [True, False])
def test_top_level_recursive_is_rejected(value):
    with pytest.raises(OcrError, match="Unknown config key"):
        resolve_config(overrides={"recursive": value})


def test_per_source_recursive_defaults_and_overlay(tmp_path):
    put(
        tmp_path / "home/config.yaml",
        {"sources": {"receipts": {"recursive": True}, "catalogs": {}}},
    )
    put(tmp_path / "explicit.yaml", {"sources": {"receipts": {"recursive": False}}})
    resolved = resolve_config(tmp_path / "explicit.yaml")
    assert not resolved.config.sources["receipts"].recursive
    assert not resolved.config.sources["catalogs"].recursive
    assert resolved.winning_sources["sources.receipts.recursive"] == str(tmp_path / "explicit.yaml")
    assert resolved.winning_sources["sources.catalogs.recursive"] == "built-in"
    assert "recursive" not in resolved.values


@pytest.mark.parametrize("settings", [{}, {"sources": {}}])
def test_default_queue_is_readonly_and_reports_builtin_paths(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    put(tmp_path / "explicit.yaml", settings)
    before = set(tmp_path.rglob("*"))
    resolved = resolve_config(tmp_path / "explicit.yaml")
    source = resolved.config.sources["default"]
    assert source.enabled and not source.recursive
    assert source.after_success == "keep" and source.output_suffix == ""
    assert resolved.winning_sources["sources.default.input_dir"] == "built-in"
    assert resolved.winning_sources["sources.default.output_dir"] == "built-in"
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("enabled", [True, False])
def test_explicit_sources_never_include_implicit_default(enabled):
    resolved = resolve_config(
        overrides={
            "sources": {
                "receipts": {
                    "input_dir": "incoming",
                    "output_dir": "searchable",
                    "enabled": enabled,
                }
            }
        }
    )
    assert set(resolved.config.sources) == {"receipts"}
    assert not any(key.startswith("sources.default.") for key in resolved.winning_sources)


def test_empty_overlay_does_not_add_default_queue(tmp_path):
    put(
        tmp_path / "home/config.yaml",
        {"sources": {"receipts": {"input_dir": "incoming", "output_dir": "searchable"}}},
    )
    put(tmp_path / "explicit.yaml", {"sources": {}})
    assert set(resolve_config(tmp_path / "explicit.yaml").config.sources) == {"receipts"}
