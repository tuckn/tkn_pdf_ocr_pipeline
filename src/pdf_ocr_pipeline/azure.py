from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential

from .auth import SCOPE, BrowserCredential
from .config import AzureConfig
from .errors import OcrError

MODEL_ID = "prebuilt-read"
LOGGER = logging.getLogger("pdf_ocr_pipeline")


class Provider(Protocol):
    def prepare(self) -> None: ...
    def submit(self, data: bytes) -> str: ...
    def collect(self, operation_url: str) -> tuple[bytes, list[int]]: ...
    def close(self) -> None: ...


class AzureRead:
    def __init__(
        self, config: AzureConfig, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not config.endpoint:
            raise OcrError("Set azure.endpoint in config; run config init and config show")
        self.config = config
        self.endpoint = config.endpoint.rstrip("/")
        self.credential: TokenCredential | None = None
        if config.auth_mode == "browser":
            self.credential = BrowserCredential(config)
        elif config.auth_mode == "default_credential":
            self.credential = DefaultAzureCredential(
                exclude_cli_credential=True,
                exclude_developer_cli_credential=True,
                exclude_powershell_credential=True,
                exclude_interactive_browser_credential=True,
                exclude_shared_token_cache_credential=True,
                exclude_visual_studio_code_credential=True,
                exclude_broker_credential=True,
            )
        self.client = httpx.Client(
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def _headers(self) -> dict[str, str]:
        if self.config.auth_mode == "key":
            key = os.environ.get(self.config.key_env, "").strip()
            if not key:
                raise OcrError(
                    f"Set environment variable {self.config.key_env} for key authentication"
                )
            return {"Ocp-Apim-Subscription-Key": key}
        try:
            assert self.credential is not None
            token = self.credential.get_token(SCOPE)
            return {"Authorization": f"Bearer {token.token}"}
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(
                "Azure authentication failed; configure an environment, workload, or managed "
                "identity for default_credential. External CLI credentials are disabled"
            ) from exc

    def prepare(self) -> None:
        self._headers()

    def submit(self, data: bytes) -> str:
        params = {"api-version": self.config.api_version, "output": "pdf"}
        if self.config.locale:
            params["locale"] = self.config.locale
        try:
            response = self.client.post(
                f"{self.endpoint}/documentintelligence/documentModels/{MODEL_ID}:analyze",
                params=params,
                headers={**self._headers(), "Content-Type": "application/pdf"},
                content=data,
            )
        except httpx.HTTPError as exc:
            raise OcrError(
                "Submission response was not received; acceptance is uncertain. "
                "No automatic resubmission was made; inspect state before --retry-uncertain"
            ) from exc
        if response.status_code != 202:
            # Even errors are not automatically retried: ambiguous POSTs can incur duplicate cost.
            raise OcrError(
                f"Azure submission returned HTTP {response.status_code}. "
                "Check endpoint, authentication, role, tier and limits; then use --retry-uncertain"
            )
        operation = response.headers.get("Operation-Location", "")
        self._validate_operation(operation)
        return str(operation)

    def _validate_operation(self, operation: str) -> None:
        try:
            parsed = urlsplit(operation)
        except ValueError as exc:
            raise OcrError("Invalid Azure operation URL") from exc
        root = urlsplit(self.endpoint)
        pattern = rf"/documentintelligence/documentModels/{MODEL_ID}/analyzeResults/[A-Za-z0-9-]+"
        if (
            parsed.scheme != root.scheme
            or parsed.netloc != root.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not re.fullmatch(pattern, parsed.path)
            or parse_qs(parsed.query) != {"api-version": [self.config.api_version]}
        ):
            raise OcrError("Invalid or untrusted Azure operation URL; refusing to send credentials")

    def _get(self, url: str, deadline: float) -> httpx.Response:
        for attempt in range(self.config.max_get_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrError("Azure polling timed out; rerun to resume the saved operation")
            response = None
            try:
                response = self.client.get(
                    url,
                    headers=self._headers(),
                    timeout=min(self.config.request_timeout_seconds, remaining),
                )
                if response.status_code == 200:
                    return response
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    if response.status_code == 404:
                        raise OcrError(
                            "Azure result is unavailable/expired; inspect state, then use "
                            "--retry-uncertain to submit again (additional charge)"
                        )
                    raise OcrError(f"Azure result request returned HTTP {response.status_code}")
            except httpx.HTTPError:
                pass
            if attempt == self.config.max_get_retries:
                raise OcrError(
                    "Azure result request failed after bounded GET retries; rerun to resume"
                )
            delay = float(2**attempt)
            if response is not None:
                with contextlib.suppress(ValueError):
                    delay = max(delay, float(response.headers.get("Retry-After", delay)))
            self._wait(min(delay, 60), deadline)
        raise AssertionError("unreachable")

    @staticmethod
    def _wait(seconds: float, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OcrError("Azure polling timed out; rerun to resume the saved operation")
        time.sleep(min(seconds, remaining))

    def collect(self, operation_url: str) -> tuple[bytes, list[int]]:
        self._validate_operation(operation_url)
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_progress = 0.0
        while True:
            response = self._get(operation_url, deadline)
            try:
                result: dict[str, Any] = response.json()
                status = result["status"]
                if not isinstance(status, str):
                    raise ValueError("invalid status")
            except (ValueError, KeyError, TypeError) as exc:
                raise OcrError("Azure returned an invalid analysis response") from exc
            if status == "succeeded":
                break
            if status in {"failed", "canceled"}:
                raise OcrError(
                    "Azure analysis failed; inspect service status before --retry-uncertain"
                )
            if status not in {"notStarted", "running"}:
                raise OcrError("Azure returned an unsupported operation status")
            if time.monotonic() - last_progress >= 30:
                LOGGER.info("Waiting for Azure OCR completion")
                last_progress = time.monotonic()
            self._wait(self.config.poll_interval_seconds, deadline)
        try:
            analysis = result["analyzeResult"]
            if analysis["modelId"] != MODEL_ID:
                raise ValueError("model mismatch")
            pages = analysis["pages"]
            if not isinstance(pages, list) or not pages:
                raise ValueError("missing pages")
            numbers = [page["pageNumber"] for page in pages]
            if numbers != list(range(1, len(pages) + 1)):
                raise ValueError("incomplete pages")
            text_pages = [page["pageNumber"] for page in pages if page.get("words")]
        except (KeyError, TypeError, ValueError) as exc:
            raise OcrError("Azure analysis result has invalid model/page metadata") from exc
        parsed = urlsplit(operation_url)
        pdf_url = urlunsplit(parsed._replace(path=parsed.path + "/pdf"))
        pdf_response = self._get(pdf_url, deadline)
        if "application/pdf" not in pdf_response.headers.get("Content-Type", "").lower():
            raise OcrError("Azure download did not return application/pdf")
        data = pdf_response.content
        from .pdf import inspect_pdf

        if inspect_pdf(data).pages != len(pages):
            raise OcrError("Azure analysis page count does not match downloaded PDF")
        return data, text_pages

    def close(self) -> None:
        self.client.close()
        if self.credential is not None:
            close = getattr(self.credential, "close", None)
            if close:
                close()
