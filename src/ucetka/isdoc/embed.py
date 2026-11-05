from __future__ import annotations

from io import BytesIO

import pikepdf

ISDOC_FILENAME = "invoice.isdoc"


def embed_isdoc(pdf_bytes: bytes, isdoc_xml: bytes) -> bytes:
    """Embed ISDOC XML podle ISDOC 6.0.2 §3.2 (ISDOC.PDF).

    Jméno přílohy je specifikací pevně `invoice.isdoc` (/F i /UF).
    AFRelationship=/Alternative: PDF i XML vznikají nezávisle z Pydantic modelu.
    """
    pdf = pikepdf.Pdf.open(BytesIO(pdf_bytes))
    fs = pikepdf.AttachedFileSpec(
        pdf,
        isdoc_xml,
        description="ISDOC Invoice",
        filename=ISDOC_FILENAME,
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments[ISDOC_FILENAME] = fs

    # Pro PDF/A-3 — vytvořit /AF entry v Catalog (Associated Files).
    catalog = pdf.Root
    af_array = catalog.get("/AF", pikepdf.Array())
    af_array.append(fs.obj)
    catalog["/AF"] = af_array

    out = BytesIO()
    pdf.save(out, linearize=False)
    return out.getvalue()


def list_attachments(pdf_bytes: bytes) -> dict[str, int]:
    pdf = pikepdf.Pdf.open(BytesIO(pdf_bytes))
    return {name: len(bytes(fs.get_file().read_bytes())) for name, fs in pdf.attachments.items()}


def extract_attachment(pdf_bytes: bytes, filename: str = ISDOC_FILENAME) -> bytes:
    pdf = pikepdf.Pdf.open(BytesIO(pdf_bytes))
    fs = pdf.attachments[filename]
    return bytes(fs.get_file().read_bytes())
