from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from pdf_ocr_pipeline.config import resolve_config


def make_pdf(
    text: str = "",
    pages: int = 1,
    *,
    rotate: bool = False,
    hidden: bool = False,
    image: bool = True,
) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=240, height=320)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    ),
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 14 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
        resources = page.setdefault(NameObject("/Resources"), DictionaryObject())
        content = page.get_contents()
        data = content.get_data() if content is not None else b""
        if hidden:
            data = b"3 Tr " + data
        if image:
            picture = DecodedStreamObject()
            picture.set_data(bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]))
            picture.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(2),
                    NameObject("/Height"): NumberObject(2),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im0"): writer._add_object(picture.flate_encode())}
            )
            data = b"q 240 0 0 320 0 0 cm /Im0 Do Q\n" + data
        stream = DecodedStreamObject()
        stream.set_data(data)
        page[NameObject("/Contents")] = writer._add_object(stream)
        if rotate:
            page.rotate(90)
    data = BytesIO()
    writer.write(data)
    return data.getvalue()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    import pdf_ocr_pipeline.config as module

    monkeypatch.setattr(module, "user_config_path", lambda: tmp_path / "home/config.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_OCR_KEY", "synthetic-key")


@pytest.fixture
def config(tmp_path):
    return resolve_config(
        overrides={
            "input_dir": str(tmp_path / "input"),
            "output_dir": str(tmp_path / "output"),
            "state_dir": str(tmp_path / "state"),
            "min_age_seconds": 0,
            "azure": {
                "endpoint": "https://example.cognitiveservices.azure.com",
                "auth_mode": "key",
                "key_env": "TEST_OCR_KEY",
            },
        }
    ).config


class FakeProvider:
    def __init__(self, output=None, *, fail_submit=False, fail_collect=False, callback=None):
        self.output = output or make_pdf("Receipt 123", hidden=True)
        self.fail_submit = fail_submit
        self.fail_collect = fail_collect
        self.callback = callback
        self.submissions = []
        self.collections = 0
        self.preparations = 0

    def prepare(self):
        self.preparations += 1

    def submit(self, data):
        from pdf_ocr_pipeline.errors import OcrError

        self.submissions.append(data)
        if self.fail_submit:
            raise OcrError("uncertain")
        return "https://example.cognitiveservices.azure.com/documentintelligence/documentModels/prebuilt-read/analyzeResults/job?api-version=2024-11-30"

    def collect(self, operation_url):
        from pdf_ocr_pipeline.errors import OcrError

        self.collections += 1
        if self.fail_collect:
            raise OcrError("temporary result failure")
        if self.callback:
            self.callback()
        return self.output, [1]

    def close(self):
        pass
