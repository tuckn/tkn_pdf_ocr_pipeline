"""Browser sign-in with an encrypted cache isolated to this application."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from azure.core.credentials import AccessToken
from azure.identity import (
    AuthenticationRecord,
    AuthenticationRequiredError,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

from .config import APPLICATION_ID, AzureConfig
from .errors import OcrError
from .io_utils import atomic_write, sha256

SCOPE = "https://cognitiveservices.azure.com/.default"
BROWSER_TIMEOUT_SECONDS = 300
LOGGER = logging.getLogger("pdf_ocr_pipeline")


def authentication_record_path(config: AzureConfig) -> Path:
    resource = (config.endpoint or "").rstrip("/").lower()
    identity = sha256((resource + "|" + (config.tenant_id or "")).encode())[:24]
    return Path.home() / ".tkn" / APPLICATION_ID / "authentication" / f"{identity}.json"


class BrowserCredential:
    def __init__(self, config: AzureConfig) -> None:
        self.record_path = authentication_record_path(config)
        record = None
        try:
            if self.record_path.exists():
                try:
                    record = AuthenticationRecord.deserialize(
                        self.record_path.read_text(encoding="utf-8")
                    )
                except (ValueError, KeyError, TypeError, AttributeError):
                    LOGGER.warning("Saved account record is invalid; browser sign-in is required")
            cache_id = sha256(str(self.record_path.resolve()).encode())[:24]
            self.credential = InteractiveBrowserCredential(
                disable_automatic_authentication=True,
                authentication_record=record,
                cache_persistence_options=TokenCachePersistenceOptions(
                    name=f"tkn-pdf-ocr-{cache_id}", allow_unencrypted_storage=False
                ),
                timeout=BROWSER_TIMEOUT_SECONDS,
                **({"tenant_id": config.tenant_id} if config.tenant_id else {}),
            )
        except Exception as exc:
            raise OcrError(
                "Browser credential initialization failed; check account-record access "
                "and OS encrypted credential storage"
            ) from exc

    def authenticate(self, *scopes: str, **kwargs: Any) -> None:
        try:
            LOGGER.info(
                "Opening your browser: Azure sign-in or account selection is required "
                "(timeout: %s seconds)",
                BROWSER_TIMEOUT_SECONDS,
            )
            record = self.credential.authenticate(scopes=list(scopes), **kwargs)
            # This record contains account identifiers. Tokens stay in the encrypted SDK cache.
            atomic_write(self.record_path, record.serialize().encode("utf-8"))
        except Exception as exc:
            raise OcrError(
                "Azure browser authentication failed or timed out; complete sign-in in "
                "the browser, allow the local callback, and check encrypted cache access. "
                "Run auth login --reauthenticate to choose an account again"
            ) from exc

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        try:
            try:
                return self.credential.get_token(*scopes, **kwargs)
            except AuthenticationRequiredError:
                self.authenticate(*scopes, **kwargs)
                return self.credential.get_token(*scopes, **kwargs)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(
                "Azure browser authentication failed; run auth login --reauthenticate "
                "and check azure.tenant_id and the signed-in account"
            ) from exc

    def close(self) -> None:
        self.credential.close()
