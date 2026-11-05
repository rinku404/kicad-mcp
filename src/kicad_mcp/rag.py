"""Cited search over every datasheet in a project, like NotebookLM but local.

Each PDF in ``datasheets/`` is split into short passages that remember their
page and section. A question is matched two ways and the rankings combined:
keywords (BM25, always available) and meaning (a small local embedding model,
when fastembed is installed), so "how hot can it get" also finds "junction
temperature". The index lives in ``datasheets/.rag/`` and is refreshed
whenever a PDF is added or changed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import datasheets

MODEL = "BAAI/bge-small-en-v1.5"  # about 70 MB, runs on CPU
INDEX_VERSION = 1
CHUNK_CHARS = 700


@dataclass
class Passage:
    file: str  # path relative to the project
    page: int
    printed_page: str | None
    section: str | None
    text: str


# --------------------------------------------------------------------------- embeddings


class _FastEmbed:
    def __init__(self) -> None:
        from fastembed import TextEmbedding

        cache = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.model = TextEmbedding(MODEL, cache_dir=str(cache / "kicad-mcp" / "models"))
        self.name = MODEL

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [[round(float(x), 5) for x in v] for v in self.model.passage_embed(texts)]

    def query(self, text: str) -> list[float]:
        return list(map(float, next(iter(self.model.query_embed([text])))))


_embedder: object | None = None
_embedder_error: str | None = None


def get_embedder():
    """The local embedding model, or None (with the reason in embedder_error()) when unavailable."""
    global _embedder, _embedder_error
    if _embedder is not None:
        return _embedder
    if os.environ.get("KICAD_MCP_EMBEDDINGS", "").lower() in ("0", "off", "no", "false"):
        _embedder_error = "turned off with KICAD_MCP_EMBEDDINGS"
        return None
    if _embedder is None and _embedder_error is None:
        try:
            _embedder = _FastEmbed()
        except ImportError:
            _embedder_error = "fastembed is not installed (pip install fastembed), so only keyword search is used"
        except Exception as e:  # noqa: BLE001 - model download or runtime problems
            _embedder_error = f"the embedding model could not be loaded ({e}), so only keyword search is used"
    return _embedder


def embedder_error() -> str | None:
    return _embedder_error


# --------------------------------------------------------------------------- chunking


def chunk(doc: datasheets.Document, rel: str) -> list[Passage]:
    """Passages of about CHUNK_CHARS characters, never crossing a page, overlapping by a line."""
    out: list[Passage] = []
    for i, text in enumerate(doc.pages, start=1):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        section = datasheets.section_of(doc, i)
        cur: list[str] = []
        size = 0
        for ln in lines:
            if cur and size + len(ln) > CHUNK_CHARS:
                out.append(Passage(rel, i, doc.label(i), section, "\n".join(cur)))
                cur = cur[-1:]
                size = len(cur[0])
            cur.append(ln)
            size += len(ln) + 1
        if cur and (not out or out[-1].page != i or "\n".join(cur) != out[-1].text):
            out.append(Passage(rel, i, doc.label(i), section, "\n".join(cur)))
    return out


# --------------------------------------------------------------------------- the index


_TOKEN = re.compile(r"[a-z0-9]+(?:[.+][a-z0-9]+)*")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Index:
    def __init__(self, folder: Path) -> None:
        self.folder = folder  # the project's datasheets/ folder
        self.dir = folder / ".rag"
        self.passages: list[Passage] = []
        self.vectors: list[list[float] | None] = []
        self.model: str | None = None
        self.skipped: dict[str, str] = {}

    def _key(self, rel: str) -> str:
        return hashlib.sha1(rel.encode()).hexdigest()[:16]

    def refresh(self, project_folder: Path) -> dict:
        """Index new or changed PDFs, drop deleted ones. Returns what changed."""
        embedder = get_embedder()
        model = getattr(embedder, "name", None)
        manifest_path = self.dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("version") != INDEX_VERSION:
                manifest = {}
        except (OSError, ValueError):
            manifest = {}
        files = manifest.get("files", {})
        added, removed = [], []
        pdfs = sorted(p for p in self.folder.glob("**/*.pdf") if ".rag" not in p.parts)
        present = set()
        self.dir.mkdir(parents=True, exist_ok=True)
        for pdf in pdfs:
            rel = pdf.relative_to(project_folder).as_posix()
            present.add(rel)
            stat = pdf.stat()
            entry = files.get(rel)
            store = self.dir / f"{self._key(rel)}.json"
            fresh = (
                entry
                and entry["mtime"] == stat.st_mtime
                and entry["size"] == stat.st_size
                and store.exists()
                and (model is None or entry.get("model") == model)
            )
            if not fresh:
                try:
                    doc = datasheets.open_pdf(pdf)
                except datasheets.DatasheetError as e:
                    self.skipped[rel] = str(e)
                    files.pop(rel, None)
                    continue
                passages = chunk(doc, rel)
                vectors = embedder.passages([p.text for p in passages]) if embedder and passages else []
                store.write_text(
                    json.dumps({"passages": [p.__dict__ for p in passages], "vectors": vectors}), encoding="utf-8"
                )
                files[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "model": model if vectors else None}
                added.append(rel)
            data = json.loads(store.read_text(encoding="utf-8"))
            ps = [Passage(**p) for p in data["passages"]]
            vs = data["vectors"] if data["vectors"] and files[rel].get("model") == model else [None] * len(ps)
            self.passages += ps
            self.vectors += vs
        for rel in list(files):
            if rel not in present:
                files.pop(rel)
                (self.dir / f"{self._key(rel)}.json").unlink(missing_ok=True)
                removed.append(rel)
        manifest_path.write_text(json.dumps({"version": INDEX_VERSION, "files": files}, indent=1), encoding="utf-8")
        self.model = model
        return {"indexed": added, "removed": removed, "files": len(files)}

    # ---- search

    def _bm25(self, query: str, pool: list[int]) -> dict[int, float]:
        q = [t for t in tokens(query) if t not in datasheets._STOP] or tokens(query)
        if not q:
            return {}
        docs = {i: Counter(tokens(self.passages[i].text + " " + (self.passages[i].section or ""))) for i in pool}
        n = len(docs) or 1
        avg = sum(sum(c.values()) for c in docs.values()) / n or 1
        df = Counter(t for c in docs.values() for t in set(c) if t in q)
        scores = {}
        for i, c in docs.items():
            length = sum(c.values())
            s = 0.0
            for t in q:
                f = c.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length / avg))
            if s > 0:
                scores[i] = s
        return scores

    def _cosine(self, query: str, pool: list[int]) -> dict[int, float]:
        embedder = get_embedder()
        if embedder is None or not any(self.vectors[i] is not None for i in pool):
            return {}
        qv = embedder.query(query)
        qn = math.sqrt(sum(x * x for x in qv)) or 1
        out = {}
        for i in pool:
            v = self.vectors[i]
            if v is None:
                continue
            vn = math.sqrt(sum(x * x for x in v)) or 1
            out[i] = sum(a * b for a, b in zip(qv, v)) / (qn * vn)
        return out

    def search(self, query: str, files: set[str] | None = None, limit: int = 6) -> tuple[list[tuple[Passage, float]], str]:
        pool = [i for i, p in enumerate(self.passages) if files is None or p.file in files]
        kw = self._bm25(query, pool)
        sem = self._cosine(query, pool)
        # reciprocal rank fusion: robust to the two scores living on different scales
        fused: dict[int, float] = {}
        for scores in (kw, sem):
            for rank, i in enumerate(sorted(scores, key=lambda i: -scores[i])[:50]):
                fused[i] = fused.get(i, 0) + 1 / (60 + rank)
        best = sorted(fused, key=lambda i: (-fused[i], -kw.get(i, 0)))  # ties go to the exact wording
        picked: list[int] = []
        for i in best:
            p = self.passages[i]
            # one passage per page keeps the answer varied
            if any(self.passages[j].file == p.file and self.passages[j].page == p.page for j in picked):
                continue
            picked.append(i)
            if len(picked) >= limit:
                break
        mode = "keywords + meaning" if sem else "keywords"
        return [(self.passages[i], round(fused[i], 4)) for i in picked], mode
