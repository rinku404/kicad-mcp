"""Tiny PDF writer for tests: pages of plain text lines, optional bookmarks and page labels."""

from __future__ import annotations


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], bookmarks: list[tuple[str, int]] = (), first_label: int | None = None) -> bytes:
    """pages: lines of text per page. bookmarks: (title, 1-based page). first_label: printed number of page 1."""
    objs: list[bytes] = []

    def add(body: str | bytes) -> int:
        objs.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objs)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = len(objs) + 2 * len(pages) + (1 + len(bookmarks) if bookmarks else 0) + 1
    page_ids = []
    for lines in pages:
        ops = ["BT /F1 10 Tf 14 TL 50 780 Td"] + [f"({_esc(ln)}) Tj T*" for ln in lines] + ["ET"]
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
            )
        )
    outline_id = None
    if bookmarks:
        outline_id = len(objs) + 1
        first, last = outline_id + 1, outline_id + len(bookmarks)
        add(f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {len(bookmarks)} >>")
        for i, (title, page) in enumerate(bookmarks):
            me = outline_id + 1 + i
            links = f" /Prev {me - 1} 0 R" if i else ""
            links += f" /Next {me + 1} 0 R" if i < len(bookmarks) - 1 else ""
            add(f"<< /Title ({_esc(title)}) /Parent {outline_id} 0 R{links} /Dest [{page_ids[page - 1]} 0 R /Fit] >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>") == pages_id
    catalog = f"<< /Type /Catalog /Pages {pages_id} 0 R"
    if outline_id:
        catalog += f" /Outlines {outline_id} 0 R"
    if first_label is not None:
        catalog += f" /PageLabels << /Nums [0 << /S /D /St {first_label} >>] >>"
    root = add(catalog + " >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, root, xref)
    return bytes(out)
