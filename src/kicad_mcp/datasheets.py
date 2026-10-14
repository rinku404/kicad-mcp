"""Datasheets: download them into the project, then search and read them by page."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Several distributor and manufacturer sites refuse obvious bots, so look like a browser.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
    "Accept": "application/pdf,*/*;q=0.8",
}
MAX_BYTES = 80 * 1024 * 1024


class DatasheetError(RuntimeError):
    pass


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("._") or "datasheet"


def download(url: str, dest_dir: Path, name: str, _hops: int = 0) -> Path:
    """Download ``url`` to ``dest_dir/<name>.pdf`` and return the path.

    Raises DatasheetError when the server does not return a PDF (for example a
    cookie wall or a product page), so the caller can ask the user to save it by hand.
    """
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith(("http://", "https://")):
        raise DatasheetError(f"not a web address: {url!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{safe_name(name)}.pdf"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_BYTES + 1)
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        raise DatasheetError(f"HTTP {e.code} downloading {url}") from e
    except urllib.error.URLError as e:
        raise DatasheetError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from e
    if len(data) > MAX_BYTES:
        raise DatasheetError("datasheet is larger than 80 MB")
    if not data.startswith(b"%PDF"):
        # some links land on an HTML page that embeds or links the real PDF
        m = re.search(rb'(?:href|src)="([^"]+\.pdf[^"]*)"', data[:500_000], re.IGNORECASE)
        if m and _hops < 2:
            nested = urllib.parse.urljoin(final_url, m.group(1).decode(errors="ignore"))
            if nested != url:
                return download(nested, dest_dir, name, _hops + 1)
        raise DatasheetError(
            f"{urllib.parse.urlsplit(final_url).netloc} did not return a PDF (it may need a browser). "
            f"Open {url} and save the PDF as {target}"
        )
    target.write_bytes(data)
    return target


# --------------------------------------------------------------------------- reading


@dataclass
class Document:
    path: Path
    pages: list[str]  # plain text per page, index 0 is PDF page 1
    labels: list[str]  # printed page labels, when the PDF defines them
    outline: list[dict]  # bookmarks: {"title", "page", "level"}

    def label(self, page: int) -> str | None:
        lab = self.labels[page - 1] if page - 1 < len(self.labels) else ""
        return lab if lab and lab != str(page) else None


_docs: dict[tuple[str, float], Document] = {}


def _reader(path: Path):
    try:
        import pypdf
    except ImportError as e:  # pragma: no cover - declared dependency
        raise DatasheetError("reading PDFs needs the pypdf package (pip install pypdf)") from e
    try:
        return pypdf.PdfReader(str(path))
    except Exception as e:
        raise DatasheetError(f"could not open {path.name} as a PDF: {e}") from e


def open_pdf(path: Path) -> Document:
    """Text, page labels and bookmarks of a PDF (cached until the file changes)."""
    path = Path(path)
    if not path.exists():
        raise DatasheetError(f"file not found: {path}")
    key = (str(path.resolve()), path.stat().st_mtime)
    if key in _docs:
        return _docs[key]
    reader = _reader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one broken page should not hide the rest
            pages.append("")
    try:
        labels = list(reader.page_labels)
    except Exception:  # noqa: BLE001
        labels = []
    outline: list[dict] = []

    def walk(items, level: int) -> None:
        for it in items:
            if isinstance(it, list):
                walk(it, level + 1)
                continue
            try:
                page = reader.get_destination_page_number(it) + 1
            except Exception:  # noqa: BLE001
                page = None
            outline.append({"title": str(getattr(it, "title", "")).strip(), "page": page, "level": level})

    try:
        walk(reader.outline, 0)
    except Exception:  # noqa: BLE001
        outline = []
    if not any(p.strip() for p in pages):
        raise DatasheetError(f"{path.name} has no text layer (it is probably scanned images), so it cannot be searched")
    doc = Document(path, pages, labels, outline)
    _docs[key] = doc
    return doc


def page_layout(path: Path, page: int) -> str:
    """One page with its columns kept aligned, which reads pin and electrical tables better."""
    reader = _reader(path)
    try:
        return reader.pages[page - 1].extract_text(extraction_mode="layout") or ""
    except Exception as e:
        raise DatasheetError(f"could not lay out page {page}: {e}") from e


def parse_pages(spec: str, count: int) -> list[int]:
    """'3', '3-5', '1,4,7-8' -> page numbers within 1..count."""
    pages: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise DatasheetError(f"bad page range {part!r}; use e.g. 3, 3-5 or 1,4,7-8")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a > b:
            a, b = b, a
        pages += [p for p in range(a, b + 1) if 1 <= p <= count]
    if not pages:
        raise DatasheetError(f"no such pages; the datasheet has {count}")
    return list(dict.fromkeys(pages))


_STOP = {"a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "is", "are", "where", "what", "how",
         "which", "does", "do", "with", "find", "about", "info", "information"}
_WORD = re.compile(r"[A-Za-z0-9_+\-./%µΩ]+")


def search(doc: Document, query: str, limit: int = 8) -> list[dict]:
    """Pages that best match ``query``, each with the lines that matched.

    Pages with every query word come first (if there are none, pages with most
    of them); pages where the words share a line, or that a bookmark names, rank higher.
    """
    words = [w.lower() for w in _WORD.findall(query)]
    words = [w for w in words if w not in _STOP] or words
    if not words:
        raise DatasheetError("empty search")
    # words match at the start of a word ("decoupl" finds "decoupling"); short ones must match whole
    pats = [
        re.compile(r"(?<![a-z0-9])" + re.escape(w) + (r"(?![a-z0-9])" if len(w) <= 2 else ""), re.IGNORECASE)
        for w in words
    ]
    bookmarks: dict[int, list[str]] = {}
    for b in doc.outline:
        if b["page"]:
            bookmarks.setdefault(b["page"], []).append(b["title"])
    found = [sum(bool(p.search(text)) for p in pats) for text in doc.pages]
    need = len(pats) if len(pats) in found else max(max(found, default=0), (len(pats) + 1) // 2, 1)
    hits = []
    for i, text in enumerate(doc.pages, start=1):
        if found[i - 1] < need:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        scored = []
        for j, ln in enumerate(lines):
            n = sum(bool(p.search(ln)) for p in pats)
            if n:
                scored.append((n, j))
        scored.sort(key=lambda s: (-s[0], s[1]))
        best_line = scored[0][0] if scored else 0
        score = best_line * 10 + sum(len(p.findall(text)) for p in pats)
        titles = bookmarks.get(i, [])
        if any(all(p.search(t) for p in pats) for t in titles):
            score += 50
        keep = sorted({k for _, j in scored[:4] for k in range(max(0, j - 1), min(len(lines), j + 2))})
        runs: list[list[int]] = []
        for k in keep:
            if runs and k == runs[-1][-1] + 1:
                runs[-1].append(k)
            else:
                runs.append([k])
        snippets = [" | ".join(lines[k] for k in run) for run in runs]
        hit = {"page": i, "score": score, "snippets": snippets}
        if found[i - 1] < len(pats):
            hit["matched_words"] = f"{found[i - 1]} of {len(pats)}"
        if doc.label(i):
            hit["printed_page"] = doc.label(i)
        if titles:
            hit["sections"] = titles
        hits.append(hit)
    hits.sort(key=lambda h: (-h["score"], h["page"]))
    for h in hits:
        del h["score"]
    return hits[:limit]


def section_of(doc: Document, page: int) -> str | None:
    """The last bookmark that starts on or before ``page``."""
    current = None
    for b in doc.outline:
        if b["page"] and b["page"] <= page and (current is None or b["page"] >= current["page"]):
            current = b
    return current["title"] if current else None
