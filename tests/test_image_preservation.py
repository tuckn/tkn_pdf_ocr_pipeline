from io import BytesIO

import pytest
from conftest import FakeProvider, make_pdf
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

from pdf_ocr_pipeline.errors import OcrError
from pdf_ocr_pipeline.pdf import inspect_pdf, merge_ocr, prepare_upload, select_pages
from pdf_ocr_pipeline.pipeline import Options, process_file


def write(writer):
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def join(*documents):
    writer = PdfWriter()
    for data in documents:
        writer.append(PdfReader(BytesIO(data)))
    return write(writer)


def text(data, page=0):
    return PdfReader(BytesIO(data)).pages[page].extract_text()


def pixels(data, page=0):
    from contextlib import closing

    import pypdfium2 as pdfium

    with (
        pdfium.PdfDocument(data) as document,
        closing(document[page]) as pdf_page,
        closing(pdf_page.render(scale=2)) as bitmap,
        bitmap.to_pil() as image,
    ):
        return image.size, image.tobytes()


def test_page_classification_and_explicit_redo():
    data = join(
        make_pdf(),
        make_pdf("Old OCR", hidden=True),
        make_pdf("Word text"),
        make_pdf(image=False),
        make_pdf("native without image", image=False),
    )
    info = inspect_pdf(data)
    assert info.page_kinds == ["scan", "ocr_text", "native_text", "no_scan_image", "native_text"]
    assert select_pages(info) == [1]
    assert select_pages(info, redo_ocr=True) == [1, 2]


def test_mixed_pdf_submits_only_scans_keeps_native_and_existing_ocr(config):
    original = join(make_pdf("Word text"), make_pdf(), make_pdf("Old OCR", hidden=True))
    source = config.input_dir / "mixed.pdf"
    source.parent.mkdir()
    source.write_bytes(original)
    destination = config.output_dir / "result.pdf"
    provider = FakeProvider()
    result = process_file(source, destination, config, Options(), provider_factory=lambda: provider)
    assert result["ocr_pages"] == [2]
    assert inspect_pdf(provider.submissions[0]).pages == 1
    output = destination.read_bytes()
    assert text(output, 0) == text(original, 0)
    assert "Receipt 123" in text(output, 1)
    assert text(output, 2) == text(original, 2)
    assert inspect_pdf(output).image_hashes == inspect_pdf(original).image_hashes
    assert all(pixels(original, n) == pixels(output, n) for n in range(3))


@pytest.mark.parametrize("native", [False, True])
def test_skip_without_provider_and_overwrite_does_not_enable_redo(config, native):
    source = config.input_dir / "existing.pdf"
    source.parent.mkdir()
    source.write_bytes(make_pdf("Existing", hidden=not native))
    result = process_file(
        source,
        config.output_dir / "out.pdf",
        config,
        Options(overwrite=True),
        provider_factory=lambda: pytest.fail("No authentication or upload"),
    )
    assert result["status"] == "skipped"
    if native:
        result = process_file(
            source,
            config.output_dir / "out.pdf",
            config,
            Options(redo_ocr=True),
            provider_factory=lambda: pytest.fail("No native OCR"),
        )
        assert result["status"] == "skipped"


def test_redo_removes_old_text_and_retains_images_metadata_links(config):
    reader = PdfReader(BytesIO(make_pdf("OLD_PRIVATE_OCR", hidden=True)))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.add_metadata({"/Title": "Original title"})
    writer.add_outline_item("Original bookmark", 0)
    writer.add_annotation(
        0,
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): RectangleObject((10, 10, 20, 20)),
                NameObject("/A"): DictionaryObject(
                    {
                        NameObject("/S"): NameObject("/URI"),
                        NameObject("/URI"): TextStringObject("https://example.com"),
                    }
                ),
            }
        ),
    )
    original = write(writer)
    upload = prepare_upload(original, [1])
    assert text(upload) == ""
    output = merge_ocr(original, make_pdf("NEW_OCR", hidden=True), [1])
    assert "OLD_PRIVATE_OCR" not in text(output)
    assert "NEW_OCR" in text(output)
    assert b"OLD_PRIVATE_OCR" not in output
    assert inspect_pdf(output).image_hashes == inspect_pdf(original).image_hashes
    assert pixels(original) == pixels(output)
    reader = PdfReader(BytesIO(output))
    assert reader.metadata.title == "Original title"
    assert reader.outline[0].title == "Original bookmark"
    assert reader.pages[0]["/Annots"][0].get_object()["/A"]["/URI"] == "https://example.com"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("unit", [1, 2])
def test_rotation_crop_userunit_and_overlay_coordinates(rotation, unit):
    writer = PdfWriter()
    page = writer.add_page(PdfReader(BytesIO(make_pdf("Old", hidden=True))).pages[0])
    page.cropbox = RectangleObject((10, 20, 230, 300))
    page.rotate(rotation)
    page[NameObject("/UserUnit")] = NumberObject(unit)
    original = write(writer)
    width, height = inspect_pdf(original).sizes[0]
    azure = PdfWriter()
    azure_page = azure.add_page(PdfReader(BytesIO(make_pdf("New", hidden=True))).pages[0])
    azure_page.mediabox = RectangleObject((0, 0, width, height))
    recognized = write(azure)
    output = merge_ocr(original, recognized, [1])
    assert inspect_pdf(output).sizes == [(width, height)]
    assert pixels(original) == pixels(output)
    assert text(output).strip() == "New"
    # Check a known glyph origin in displayed coordinates, including the grafted Form matrix.
    from pdf_ocr_pipeline.pdf import _display

    page = PdfReader(BytesIO(output)).pages[0]
    content = page.get_contents().operations
    matrix = next(operands for operands, op in reversed(content) if op == b"cm")
    from pypdf import Transformation

    actual = _display(page).apply_on(Transformation(tuple(map(float, matrix))).apply_on((20, 200)))
    assert actual == pytest.approx((20, 200), abs=1e-7)


def test_shared_nested_forms_do_not_change_other_page():
    writer = PdfWriter()
    old = PdfReader(BytesIO(make_pdf("OLD", hidden=True))).pages[0]
    form = DecodedStreamObject()
    form.set_data(old.get_contents().get_data())
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): RectangleObject((0, 0, 240, 320)),
            NameObject("/Resources"): old["/Resources"].clone(writer),
        }
    )
    reference = writer._add_object(form)
    for _ in range(2):
        page = writer.add_blank_page(width=240, height=320)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Shared"): reference})}
        )
        content = DecodedStreamObject()
        content.set_data(b"/Shared Do")
        page[NameObject("/Contents")] = writer._add_object(content)
    original = write(writer)
    assert inspect_pdf(original).page_kinds == ["ocr_text", "ocr_text"]
    output = merge_ocr(original, make_pdf("NEW", hidden=True), [1])
    assert "NEW" in text(output, 0) and "OLD" not in text(output, 0)
    assert "OLD" in text(output, 1)
    assert pixels(output, 0) == pixels(original, 0)
    assert pixels(output, 1) == pixels(original, 1)


def test_reject_visible_provider_text():
    with pytest.raises(OcrError, match="visible text"):
        merge_ocr(make_pdf(), make_pdf("Visible"), [1])


def test_unsupported_actualtext_kept_without_redo():
    writer = PdfWriter()
    page = writer.add_page(PdfReader(BytesIO(make_pdf("Old", hidden=True))).pages[0])
    content = DecodedStreamObject()
    content.set_data(
        b"/Span << /ActualText (Alternate old text) >> BDC\n"
        + page.get_contents().get_data()
        + b"\nEMC"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    info = inspect_pdf(write(writer))
    assert info.page_kinds == ["unsupported"]
    assert select_pages(info, redo_ocr=True) == []


def test_second_redo_of_our_output_has_no_old_text_or_extra_images():
    first = merge_ocr(make_pdf(), make_pdf("FIRST", hidden=True), [1])
    assert inspect_pdf(first).page_kinds == ["ocr_text"]
    second = merge_ocr(first, make_pdf("SECOND", hidden=True), [1])
    assert text(second).strip() == "SECOND"
    assert b"FIRST" not in second
    assert inspect_pdf(second).image_hashes == inspect_pdf(first).image_hashes
    assert pixels(second) == pixels(first)


def test_adobe_confidence_markers_and_indirect_font_resources():
    writer = PdfWriter()
    page = writer.add_page(PdfReader(BytesIO(make_pdf("OLD", hidden=True))).pages[0])
    resources = page["/Resources"]
    resources[NameObject("/Font")] = writer._add_object(resources["/Font"])
    content = DecodedStreamObject()
    content.set_data(b"/Suspect << /Conf 0.5 >> BDC\n" + page.get_contents().get_data() + b"\nEMC")
    page[NameObject("/Contents")] = writer._add_object(content)
    original = write(writer)
    assert inspect_pdf(original).page_kinds == ["ocr_text"]
    output = merge_ocr(original, make_pdf("NEW", hidden=True), [1])
    assert text(output).strip() == "NEW"
    assert pixels(output) == pixels(original)


def test_inline_image_bytes_and_appearance_preserved():
    writer = PdfWriter()
    page = writer.add_blank_page(width=240, height=320)
    content = DecodedStreamObject()
    content.set_data(
        b"q 240 0 0 320 0 0 cm BI /W 2 /H 2 /CS /RGB /BPC 8 ID "
        + bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
        + b"\nEI Q"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    original = write(writer)
    assert inspect_pdf(original).page_kinds == ["scan"]
    output = merge_ocr(original, make_pdf("NEW", hidden=True), [1])
    assert inspect_pdf(original).image_hashes == inspect_pdf(output).image_hashes
    assert pixels(original) == pixels(output)


def test_redo_option_in_pipeline_and_readonly_plan(config):
    original = join(make_pdf("Native"), make_pdf("OLD", hidden=True))
    source = config.input_dir / "mixed.pdf"
    source.parent.mkdir()
    source.write_bytes(original)
    output = config.output_dir / "result.pdf"
    before = list(config.input_dir.parent.rglob("*"))
    plan = process_file(
        source,
        output,
        config,
        Options(dry_run=True, redo_ocr=True),
        provider_factory=lambda: pytest.fail("No provider during dry run"),
    )
    assert plan["ocr_pages"] == [2]
    assert plan["preserve_images"]
    assert list(config.input_dir.parent.rglob("*")) == before
    provider = FakeProvider()
    result = process_file(
        source, output, config, Options(redo_ocr=True), provider_factory=lambda: provider
    )
    assert result["ocr_pages"] == [2]
    assert inspect_pdf(provider.submissions[0]).text_pages == []
    assert text(output.read_bytes(), 0) == "Native"
    assert text(output.read_bytes(), 1) == "Receipt 123"
    assert source.read_bytes() == original


def test_cli_redo_is_explicit_and_legacy_options_rejected():
    from pdf_ocr_pipeline.cli import parser

    args = parser().parse_args(["convert", "scan.pdf", "--output", "out.pdf", "--redo-ocr"])
    assert args.redo_ocr and not args.overwrite
    for option in ("--existing-text", "--redo-dpi"):
        with pytest.raises(OcrError):
            parser().parse_args(["convert", "scan.pdf", "--output", "out.pdf", option, "redo"])
