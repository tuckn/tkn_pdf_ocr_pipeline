"""Source-aligned OCR text export from Azure's prebuilt-read response."""

from __future__ import annotations

import json
from typing import Any

from .errors import OcrError


def build_text_json(
    analysis: dict[str, Any],
    *,
    source_hash: str,
    source_name: str,
    source_pages: int,
    analyzed_pages: list[int],
    api_version: str,
) -> bytes:
    pages = analysis.get("pages")
    if not isinstance(pages, list) or len(pages) != len(analyzed_pages):
        raise OcrError("Azure OCR page data is incomplete for JSON export")
    results: list[dict[str, Any]] = []
    for original_number, page in zip(analyzed_pages, pages, strict=True):
        lines = page.get("lines", [])
        if not isinstance(lines, list):
            raise OcrError("Azure OCR line data is invalid")
        exported_lines = []
        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("content"), str):
                raise OcrError("Azure OCR line data is invalid")
            exported_lines.append(
                {
                    "text": line["content"],
                    "boundingBox": line.get("polygon"),
                }
            )
        results.append(
            {
                "page": original_number,
                "fullPageText": "\n".join(line["text"] for line in exported_lines),
                "lines": exported_lines,
            }
        )
    full_text = analysis.get("content")
    if not isinstance(full_text, str):
        full_text = "\n".join(page["fullPageText"] for page in results)
    value = {
        "schema_version": "1.0.0",
        "sourceFileName": source_name,
        "source_sha256": source_hash,
        "source_pages": source_pages,
        "analyzed_pages": analyzed_pages,
        "model_id": "prebuilt-read",
        "api_version": api_version,
        "TextRecognition": {
            "responsev2": {
                "predictionOutput": {
                    "fullText": full_text,
                    "results": results,
                }
            }
        },
        "azureAnalyzeResult": analysis,
    }
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
