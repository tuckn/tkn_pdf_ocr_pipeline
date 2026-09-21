from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from io import BytesIO
from math import isfinite
from typing import Any, Literal, cast

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import (
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    RectangleObject,
)

from .errors import OcrError

TEXT_SHOW = {b"Tj", b"TJ", b"'", b'"'}
# Text-only overlays may retain positioning, clipping, and graphics state, but never paint.
PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"}


@dataclass(frozen=True)
class PdfInfo:
    pages: int
    text_pages: list[int]
    sizes: list[tuple[float, float]]
    page_kinds: list[str] = field(default_factory=list)
    image_hashes: list[list[str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class _Analysis:
    hidden: int = 0
    visible: int = 0
    images: list[str] = field(default_factory=list)
    unsupported: bool = False


def _canonical(value: Any, depth: int = 0) -> bytes:
    """Include encoded pixels, masks, profiles and decoding parameters, ignoring object IDs."""
    if depth > 40:
        raise OcrError("Cyclic or excessively nested image resources")
    value = value.get_object() if hasattr(value, "get_object") else value
    if isinstance(value, dict):
        parts = [
            str(key).encode() + b":" + _canonical(item, depth + 1)
            for key, item in sorted(value.items())
            if key != "/Length"
        ]
        if hasattr(value, "_data"):
            parts.append(b"stream:" + value._data)
        return b"{" + b"|".join(parts) + b"}"
    if isinstance(value, list):
        return b"[" + b"|".join(_canonical(v, depth + 1) for v in value) + b"]"
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _walk(
    stream: Any,
    resources: Any,
    owner: Any,
    stats: _Analysis,
    *,
    mode: Literal["inspect", "strip", "overlay"] = "inspect",
    inherited: tuple[int, str] = (0, ""),
    ancestors: frozenset[int] = frozenset(),
) -> tuple[ContentStream, DictionaryObject]:
    resources = resources.get_object() if resources is not None else DictionaryObject()
    result_resources = DictionaryObject(resources)
    xobjects = resources.get("/XObject", DictionaryObject()).get_object()
    new_xobjects = DictionaryObject(xobjects) if mode != "overlay" else DictionaryObject()
    content = ContentStream(stream, owner)
    kept: list[tuple[Any, bytes]] = []
    state = inherited
    stack: list[tuple[int, str]] = []
    for operands, operator in content.operations:
        keep = True
        if operator == b"q":
            stack.append(state)
        elif operator == b"Q":
            if not stack:
                raise OcrError("Unbalanced PDF graphics state")
            state = stack.pop()
        elif operator == b"Tr":
            state = (int(operands[0]), state[1])
        elif operator == b"Tf":
            font = resources.get("/Font", DictionaryObject()).get_object().get(operands[0])
            subtype = str(font.get_object().get("/Subtype", "")) if font else ""
            state = (state[0], subtype)
        elif operator == b"gs":
            gs = resources.get("/ExtGState", DictionaryObject()).get_object().get(operands[0])
            if gs and "/Font" in gs.get_object():
                font = gs.get_object()["/Font"][0].get_object()
                state = (state[0], str(font.get("/Subtype", "")))
        elif operator in TEXT_SHOW:
            if state[1] in {"", "/Type3"}:
                stats.unsupported = True
            if state[0] == 3:
                stats.hidden += 1
                keep = mode != "strip"
            else:
                stats.visible += 1
                if mode == "overlay":
                    raise OcrError("Azure PDF contains visible text; cannot use as an OCR overlay")
        elif operator == b"Do":
            obj = xobjects[operands[0]].get_object()
            if "/OC" in obj:
                stats.unsupported = True
            if obj.get("/Subtype") == "/Image":
                stats.images.append(hashlib.sha256(_canonical(obj)).hexdigest())
                keep = mode != "overlay"
            elif obj.get("/Subtype") == "/Form":
                if id(obj) in ancestors or len(ancestors) > 40:
                    raise OcrError("Recursive PDF Form XObject")
                if "/StructParent" in obj or "/StructParents" in obj:
                    stats.unsupported = True
                child, child_resources = _walk(
                    obj,
                    obj.get("/Resources", resources),
                    owner,
                    stats,
                    mode=mode,
                    inherited=state,
                    ancestors=ancestors | {id(obj)},
                )
                if mode != "inspect":
                    replacement = DecodedStreamObject()
                    replacement.update(
                        {
                            k: v
                            for k, v in obj.items()
                            if k not in {"/Length", "/Filter", "/DecodeParms", "/Resources"}
                        }
                    )
                    replacement.set_data(child.get_data())
                    replacement[NameObject("/Resources")] = child_resources
                    new_xobjects[operands[0]] = (
                        owner._add_object(replacement)
                        if isinstance(owner, PdfWriter)
                        else replacement
                    )
            else:
                stats.unsupported = True
        elif operator == b"INLINE IMAGE":
            stats.images.append(hashlib.sha256(_canonical(operands)).hexdigest())
            keep = mode != "overlay"
        elif operator in {b"BDC", b"DP"}:
            properties = operands[1]
            if isinstance(properties, str):
                properties = (
                    resources.get("/Properties", DictionaryObject())
                    .get_object()
                    .get(properties, DictionaryObject())
                )
            properties = properties.get_object()
            # Adobe marks uncertain recognition with /Suspect << /Conf ... >>.
            # This carries no replacement text or optional-content visibility semantics.
            if operands[0] != "/Suspect" or set(properties) - {"/Conf"}:
                stats.unsupported = True
        if mode == "overlay" and operator in PAINT:
            # End a path without painting. Shading does not consume a path.
            if operator != b"sh":
                kept.append(([], b"n"))
            keep = False
        if keep:
            kept.append((operands, operator))
    if stack:
        raise OcrError("Unbalanced PDF graphics state")
    if mode != "inspect":
        content.operations = kept
        result_resources[NameObject("/XObject")] = new_xobjects
    return content, result_resources


def _reader(data: bytes) -> PdfReader:
    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise OcrError("Encrypted PDFs are unsupported; provide a decrypted copy")
    if not reader.pages:
        raise OcrError("PDF contains no pages")
    return reader


def inspect_pdf(data: bytes) -> PdfInfo:
    try:
        reader = _reader(data)
        text_pages, sizes, kinds, images = [], [], [], []
        for number, page in enumerate(reader.pages, 1):
            if (page.extract_text() or "").strip():
                text_pages.append(number)
            width = float(page.cropbox.width) * float(page.user_unit)
            height = float(page.cropbox.height) * float(page.user_unit)
            if page.rotation % 180:
                width, height = height, width
            if not isfinite(width) or not isfinite(height) or width <= 0 or height <= 0:
                raise OcrError(f"Invalid page dimensions on page {number}")
            sizes.append((width, height))
            stats = _Analysis()
            _walk(page.get_contents(), page.get("/Resources"), reader, stats)
            if "/StructParents" in page or "/OC" in page:
                stats.unsupported = True
            if stats.visible:
                kind = "native_text"
            elif stats.unsupported:
                kind = "unsupported"
            elif stats.images:
                kind = "ocr_text" if stats.hidden else "scan"
            else:
                kind = "no_scan_image"
            kinds.append(kind)
            images.append(stats.images)
        return PdfInfo(len(reader.pages), text_pages, sizes, kinds, images)
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError("PDF cannot be parsed safely; repair or replace the input file") from exc


def select_pages(info: PdfInfo, *, redo_ocr: bool = False) -> list[int]:
    return [
        n
        for n, kind in enumerate(info.page_kinds, 1)
        if kind == "scan" or (kind == "ocr_text" and redo_ocr)
    ]


def _strip_page(page: Any, writer: PdfWriter) -> None:
    stats = _Analysis()
    content, resources = _walk(
        page.get_contents(), page.get("/Resources"), writer, stats, mode="strip"
    )
    if stats.visible or stats.unsupported:
        raise OcrError("Cannot replace text on a native or unsupported page")
    page[NameObject("/Contents")] = writer._add_object(content)
    page[NameObject("/Resources")] = resources


def _write(writer: PdfWriter) -> bytes:
    # Old content streams must disappear, while shared resources on retained pages survive.
    writer.compress_identical_objects(remove_duplicates=False, remove_unreferenced=True)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def prepare_upload(data: bytes, pages: list[int]) -> bytes:
    try:
        reader = _reader(data)
        with PdfWriter() as writer:
            for number in pages:
                page = writer.add_page(reader.pages[number - 1], excluded_keys=["/Annots"])
                _strip_page(page, writer)
            return _write(writer)
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError("Cannot prepare image-preserving OCR upload") from exc


def _display(page: Any) -> Transformation:
    box = page.cropbox
    transform = Transformation().translate(-float(box.left), -float(box.bottom))
    transform = transform.rotate(-page.rotation)
    corners = [
        transform.apply_on((float(x), float(y)))
        for x in (box.left, box.right)
        for y in (box.bottom, box.top)
    ]
    return transform.translate(-min(p[0] for p in corners), -min(p[1] for p in corners)).scale(
        float(page.user_unit), float(page.user_unit)
    )


def _inverse(transform: Transformation) -> Transformation:
    a, b, c, d, e, f = transform.ctm
    det = a * d - b * c
    return Transformation(
        (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)
    )


def _promote_forms(form: Any, writer: PdfWriter) -> None:
    resources = form.get("/Resources", DictionaryObject()).get_object()
    xobjects = resources.get("/XObject", DictionaryObject()).get_object()
    for name, reference in list(xobjects.items()):
        child = reference.get_object()
        if child.get("/Subtype") == "/Form":
            _promote_forms(child, writer)
            if not isinstance(reference, IndirectObject):
                xobjects[name] = writer._add_object(child)


def merge_ocr(original: bytes, recognized: bytes, pages: list[int]) -> bytes:
    """Graft only invisible text/fonts onto original pages; keep encoded source images."""
    try:
        source_info = inspect_pdf(original)
        recognized_info = inspect_pdf(recognized)
        expected = PdfInfo(len(pages), [], [source_info.sizes[n - 1] for n in pages])
        validate_output(recognized, expected, [])
        reader = _reader(original)
        ocr = _reader(recognized)
        with PdfWriter() as writer:
            writer.clone_document_from_reader(reader)
            for index, number in enumerate(pages):
                target = writer.pages[number - 1]
                _strip_page(target, writer)
                overlay = ocr.pages[index]
                stats = _Analysis()
                content, resources = _walk(
                    overlay.get_contents(), overlay.get("/Resources"), ocr, stats, mode="overlay"
                )
                if stats.unsupported:
                    raise OcrError("Azure PDF uses unsupported text semantics")
                form = DecodedStreamObject()
                form.set_data(content.get_data())
                form.update(
                    {
                        NameObject("/Type"): NameObject("/XObject"),
                        NameObject("/Subtype"): NameObject("/Form"),
                        NameObject("/BBox"): RectangleObject(overlay.cropbox),
                        NameObject("/Resources"): resources,
                    }
                )
                cloned_form = form.clone(writer)
                _promote_forms(cloned_form, writer)
                reference = writer._add_object(cloned_form)
                target_resources = DictionaryObject(cast(DictionaryObject, target["/Resources"]))
                xobjects = DictionaryObject(target_resources.get("/XObject", {}).get_object())
                name = NameObject("/TknOCR")
                while name in xobjects:
                    name = NameObject(str(name) + "_")
                xobjects[name] = reference
                target_resources[NameObject("/XObject")] = xobjects
                target[NameObject("/Resources")] = target_resources
                # Map displayed OCR coordinates back to the source's unrotated coordinate space.
                sw, sh = source_info.sizes[number - 1]
                ow, oh = recognized_info.sizes[index]
                matrix = (
                    _display(overlay).scale(sw / ow, sh / oh).transform(_inverse(_display(target)))
                )
                command = " ".join(format(v, ".10f") for v in matrix.ctm)
                combined = DecodedStreamObject()
                original_content = target.get_contents()
                assert original_content is not None
                combined.set_data(
                    b"q\n"
                    + original_content.get_data()
                    + b"\nQ\nq\n"
                    + f"{command} cm {name} Do\nQ\n".encode("ascii")
                )
                target[NameObject("/Contents")] = writer._add_object(combined)
            result = _write(writer)
        result_info = validate_output(result, source_info, [])
        if result_info.image_hashes != source_info.image_hashes:
            raise OcrError("Encoded source images or their decoding settings changed")
        # Unselected pages retain their content, including native and previously OCRed text.
        result_reader = _reader(result)
        for number in set(range(1, source_info.pages + 1)) - set(pages):
            if (
                reader.pages[number - 1].extract_text()
                != result_reader.pages[number - 1].extract_text()
            ):
                raise OcrError(f"Retained text changed on page {number}")
        return result
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError("Cannot compose image-preserving OCR PDF") from exc


def validate_output(data: bytes, expected: PdfInfo, ocr_pages: list[int]) -> PdfInfo:
    info = inspect_pdf(data)
    if info.pages != expected.pages:
        raise OcrError(
            f"Output has {info.pages} pages; expected {expected.pages} (check Azure tier)"
        )
    for number, (actual, original) in enumerate(zip(info.sizes, expected.sizes, strict=True), 1):
        if any(abs(a - b) > 2 for a, b in zip(actual, original, strict=True)):
            raise OcrError(f"Output page {number} has different displayed dimensions")
    missing = sorted(set(ocr_pages) - set(info.text_pages))
    if missing:
        raise OcrError(f"OCR detected words but output lacks extractable text on pages {missing}")
    return info
