from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.identity import AuthenticationRecord, AuthenticationRequiredError
from conftest import FakeProvider, make_pdf

from pdf_ocr_pipeline import auth
from pdf_ocr_pipeline.azure import AzureRead
from pdf_ocr_pipeline.cli import main
from pdf_ocr_pipeline.config import resolve_config
from pdf_ocr_pipeline.errors import OcrError
from pdf_ocr_pipeline.pipeline import Options, process_file


@pytest.fixture
def browser_config(config, tmp_path, monkeypatch):
    monkeypatch.setattr(auth.Path, "home", lambda: tmp_path / "browser-home")
    return replace(config, azure=replace(config.azure, auth_mode="browser"))


def install_browser(monkeypatch, *, authenticated=False, failure=None):
    record = AuthenticationRecord(
        "tenant", "client", "authority", "account", "user@example.invalid"
    )
    instances = []
    options = []
    cache = {"valid": authenticated}

    def factory(**kwargs):
        options.append(kwargs)
        credential = Mock()

        def get_token(*scopes, **token_options):
            assert scopes == (auth.SCOPE,)
            if failure:
                raise failure
            if not cache["valid"] or kwargs["authentication_record"] is None:
                raise AuthenticationRequiredError(scopes, message="sign in")
            return AccessToken("test-access-token", 9999999999)

        def authenticate(**auth_options):
            assert auth_options["scopes"] == [auth.SCOPE]
            cache["valid"] = True
            kwargs["authentication_record"] = record
            return record

        credential.get_token.side_effect = get_token
        credential.authenticate.side_effect = authenticate
        instances.append(credential)
        return credential

    monkeypatch.setattr(auth, "InteractiveBrowserCredential", factory)
    return record, instances, options, cache


def test_browser_once_then_reuses_account_and_encrypted_cache(browser_config, monkeypatch):
    record, instances, options, cache = install_browser(monkeypatch)
    for _ in range(2):
        client = AzureRead(browser_config.azure, transport=httpx.MockTransport(lambda r: None))
        client.prepare()
        client.prepare()
        client.close()
    assert sum(i.authenticate.call_count for i in instances) == 1
    assert all(i.close.call_count == 1 for i in instances)
    path = auth.authentication_record_path(browser_config.azure)
    assert AuthenticationRecord.deserialize(path.read_text()).home_account_id == "account"
    assert "test-access-token" not in path.read_text()
    assert options[1]["authentication_record"].serialize() == record.serialize()
    assert options[0]["disable_automatic_authentication"] is True
    assert options[0]["cache_persistence_options"].allow_unencrypted_storage is False
    assert options[0]["cache_persistence_options"].name.startswith("tkn-pdf-ocr-")
    assert (
        options[0]["cache_persistence_options"].name == options[1]["cache_persistence_options"].name
    )
    cache["valid"] = False
    client = AzureRead(browser_config.azure)
    client.prepare()
    client.close()
    assert instances[-1].authenticate.call_count == 1


@pytest.mark.parametrize("content", ["not-json", "[]", '{"version":"unknown"}'])
def test_invalid_account_record_can_be_replaced(browser_config, monkeypatch, content):
    install_browser(monkeypatch)
    path = auth.authentication_record_path(browser_config.azure)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    credential = auth.BrowserCredential(browser_config.azure)
    try:
        credential.get_token(auth.SCOPE)
    finally:
        credential.close()
    assert AuthenticationRecord.deserialize(path.read_text()).tenant_id == "tenant"


def test_endpoint_and_tenant_isolate_cache(browser_config, monkeypatch):
    _, _, options, _ = install_browser(monkeypatch)
    first = browser_config.azure
    for cfg in (
        first,
        replace(first, tenant_id="example.onmicrosoft.com"),
        replace(first, endpoint="https://second.cognitiveservices.azure.com"),
    ):
        credential = auth.BrowserCredential(cfg)
        credential.close()
    assert options[1]["tenant_id"] == "example.onmicrosoft.com"
    assert len({o["cache_persistence_options"].name for o in options}) == 3


@pytest.mark.parametrize("stage", ["initialize", "authenticate", "persist", "token"])
def test_auth_failure_precedes_billable_submission(browser_config, monkeypatch, stage):
    record, instances, _, _ = install_browser(monkeypatch)
    if stage == "initialize":
        monkeypatch.setattr(
            auth, "InteractiveBrowserCredential", Mock(side_effect=ValueError("secret"))
        )
    elif stage == "persist":
        monkeypatch.setattr(auth, "atomic_write", Mock(side_effect=OSError("secret")))
    elif stage in {"authenticate", "token"}:
        credential = Mock()
        credential.get_token.side_effect = (
            AuthenticationRequiredError([auth.SCOPE])
            if stage == "authenticate"
            else ValueError("secret")
        )
        credential.authenticate.side_effect = ValueError("secret")
        monkeypatch.setattr(auth, "InteractiveBrowserCredential", Mock(return_value=credential))
    source = browser_config.input_dir / "scan.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(make_pdf())
    requests = []
    with pytest.raises(OcrError) as error:
        process_file(
            source,
            browser_config.output_dir / "out.pdf",
            browser_config,
            Options(),
            provider_factory=lambda: AzureRead(
                browser_config.azure, transport=httpx.MockTransport(lambda r: requests.append(r))
            ),
        )
    assert "secret" not in str(error.value)
    assert not requests
    assert not list(browser_config.state_dir.glob("jobs/*.json"))
    assert source.read_bytes() == make_pdf()


def test_default_credential_excludes_all_developer_tools(config, monkeypatch):
    import pdf_ocr_pipeline.azure as module

    factory = Mock(return_value=Mock())
    monkeypatch.setattr(module, "DefaultAzureCredential", factory)
    client = AzureRead(replace(config.azure, auth_mode="default_credential"))
    client.close()
    assert all(
        factory.call_args.kwargs[key] is True
        for key in [
            "exclude_cli_credential",
            "exclude_developer_cli_credential",
            "exclude_powershell_credential",
            "exclude_interactive_browser_credential",
            "exclude_broker_credential",
            "exclude_visual_studio_code_credential",
            "exclude_shared_token_cache_credential",
        ]
    )


def test_login_dry_run_and_config_show_do_not_touch_auth(browser_config, monkeypatch, capsys):
    monkeypatch.setattr(
        auth, "InteractiveBrowserCredential", Mock(side_effect=AssertionError("no auth"))
    )
    path = Path.cwd() / ".tkn/config.yaml"
    path.parent.mkdir()
    path.write_text('schema_version: "2.0.0"\nazure:\n  endpoint: https://example.com\n')
    before = sorted(Path.cwd().rglob("*"))
    assert main(["auth", "login", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert main(["config", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["azure"]["auth_mode"] == "browser"
    assert sorted(Path.cwd().rglob("*")) == before


def test_login_reauthenticate_without_pdf_requests(browser_config, monkeypatch, capsys):
    _, instances, _, _ = install_browser(monkeypatch)
    path = Path.cwd() / ".tkn/config.yaml"
    path.parent.mkdir()
    path.write_text('schema_version: "2.0.0"\nazure:\n  endpoint: https://example.com\n')
    for args in (["auth", "login"], ["auth", "login"], ["auth", "login", "--reauthenticate"]):
        assert main(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "authenticated"
        assert result["resource_access_checked"] is False
        assert "test-access-token" not in json.dumps(result)
    assert sum(c.authenticate.call_count for c in instances) == 2


def test_auth_change_and_release_do_not_invalidate_completed_pdf(browser_config, monkeypatch):
    import pdf_ocr_pipeline.pipeline as pipeline
    from pdf_ocr_pipeline.io_utils import sha256

    source = browser_config.input_dir / "scan.pdf"
    source.parent.mkdir()
    source.write_bytes(make_pdf())
    output = browser_config.output_dir / "out.pdf"
    first = replace(browser_config, azure=replace(browser_config.azure, auth_mode="key"))
    result = process_file(source, output, first, Options(), provider_factory=FakeProvider)
    # Authentication-only changes do not invalidate image-preserving jobs.
    old = {
        "generator": "0.3.0",
        "model": "prebuilt-read",
        "endpoint": first.azure.endpoint,
        "api_version": first.azure.api_version,
        "locale": first.azure.locale,
        "redo_ocr": False,
    }
    state = json.loads(Path(result["state"]).read_text())
    assert state["fingerprint"] == sha256(json.dumps(old, sort_keys=True).encode())
    monkeypatch.setattr(pipeline, "__version__", "0.3.1")
    again = process_file(
        source,
        output,
        browser_config,
        Options(),
        provider_factory=lambda: pytest.fail("must not authenticate or resubmit"),
    )
    assert again["status"] == "unchanged"


def test_azure_cli_config_is_rejected_with_browser_migration():
    with pytest.raises(OcrError, match="set azure.auth_mode to browser"):
        resolve_config(overrides={"azure": {"auth_mode": "azure_cli"}})
