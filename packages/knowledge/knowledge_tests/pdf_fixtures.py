"""Test PDFs built with pypdf's writer (no downloaded binaries), for P1.A3's extractor, sandbox and
step tests. Each page is a list of (text, font size) lines set in Helvetica, top to bottom.

    build_pdf(pages, outline=[(title, page_index, parent_title | None)], encrypt="pw" | "" | None,
              image_pages={i, ...}, title="metadata title")
"""

from __future__ import annotations

import io
from collections.abc import Collection, Sequence

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, StreamObject

Line = tuple[str, float]

WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa"
).split()


def body_lines(n: int, count: int = 4, size: float = 10) -> list[Line]:
    """`count` body lines for page `n`, with words that differ from page to page (so they are never
    mistaken for a running header or footer)."""
    return [
        (f"{WORDS[(n * count + k) % 16]} paragraph explains the UART pin mux {WORDS[(n + k * 3) % 16]} {n}-{k}.", size)
        for k in range(count)
    ]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(
    pages: Sequence[Sequence[Line]],
    *,
    outline: Sequence[tuple[str, int, str | None]] = (),
    encrypt: str | None = None,
    image_pages: Collection[int] = (),
    title: str | None = None,
) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for index, lines in enumerate(pages):
        page = writer.add_blank_page(612, 792)
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        ops: list[str] = []
        y = 740.0
        for text, size in lines:
            ops.append(f"BT /F1 {size} Tf 72 {y} Td ({_escape(text)}) Tj ET")
            y -= size + 8
        if index in image_pages:
            image = StreamObject()
            image._data = b"\x00\x00\x00"
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): writer._add_object(image)})
            ops.append("q 100 0 0 100 72 72 cm /Im1 Do Q")
        page[NameObject("/Resources")] = resources
        content = DecodedStreamObject()
        content.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(content)
    parents: dict[str, object] = {}
    for entry_title, page_index, parent in outline:
        parents[entry_title] = writer.add_outline_item(entry_title, page_index, parent=parents.get(parent))
    if title is not None:
        writer.add_metadata({"/Title": title})
    if encrypt is not None:
        writer.encrypt(encrypt, owner_password="owner-secret", algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def manual_pdf() -> bytes:
    """A 6-page manual: running header and footer on every page, an outline Intro (p1) > Setup (p3)
    > Pins (p4, under Setup), and an image-only last page."""
    pages: list[list[Line]] = []
    for i in range(5):
        pages.append([("SPRUIV7 AM62x Technical Reference Manual", 8), *body_lines(i), (f"Copyright 2026 Page {i + 1}", 8)])
    pages.append([("SPRUIV7 AM62x Technical Reference Manual", 8), (f"Copyright 2026 Page 6", 8)])
    return build_pdf(
        pages,
        outline=[("Intro", 0, None), ("Setup", 2, None), ("Pins", 3, "Setup")],
        image_pages={5},
        title="AM62x TRM",
    )


def flate_bomb_pdf(inflated_bytes: int = 500 * 1024 * 1024) -> bytes:
    """A one-page PDF whose content stream is FlateDecode data inflating to `inflated_bytes` zeros
    (500 MB by default, about 500 KB compressed)."""
    import zlib

    compressor = zlib.compressobj(9)
    chunk = b"\x00" * (1024 * 1024)
    parts = [compressor.compress(chunk) for _ in range(inflated_bytes // len(chunk))]
    parts.append(compressor.flush())
    data = b"".join(parts)
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    # A font resource, so text extraction really decodes the stream (pypdf skips a page without one).
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = StreamObject()
    stream._data = data
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
