"""Build the policy search index: PDFs + transcribed annexures → section chunks → Qdrant.

Usage:
    python -m ridewise.ingest          # (re)build the index
    python -m ridewise.ingest --show   # also print every chunk

Chunks are whole numbered sections (FAQ: one chunk per Q&A), never fixed-size
windows, so a clause or table is never split. Each chunk carries a contextual
header (document, version, scope, section) so that v1 and v2 refund text are
distinguishable both to the retriever and to the LLM.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import pymupdf

from ridewise.config import ROOT, settings
from ridewise.rules import load_rules

COLLECTION = "policies"
INGEST_VERSION = "2"            # bump when chunking logic changes
ANNEXURES = ROOT / "policies" / "annexures.md"

_HEADER_LINES = [
    re.compile(r"^RW-POL-\d+\s*·\s*v[\d.]+$"),
    re.compile(r"^RideWise Mobility Pvt\. Ltd\.\s*·\s*Customer policy document$"),
    re.compile(r"^Page \d+$"),
]
_SECTION_RE = re.compile(r"^(\d+)\.\s+([A-Z].*)$")          # "4. Rental charges" (not "4.1 …")
_ANNEX_RE = re.compile(r"^(Annexure\s+\w+):\s*(.*)$")
_FAQ_Q_RE = re.compile(r"^Q\.\s+")


# ------------------------------------------------------------------ PDF → lines

def _page_lines(page: pymupdf.Page) -> list[str]:
    """Page text in reading order, with tables rendered as Markdown and headers/footers removed."""
    with contextlib.redirect_stdout(io.StringIO()):          # silence PyMuPDF layout hint
        tables = page.find_tables().tables
    boxes = [pymupdf.Rect(t.bbox) for t in tables]
    items: list[tuple[float, str]] = [(t.bbox[1], t.to_markdown().strip()) for t in tables]
    for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
        centre = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
        if not any(centre in b for b in boxes):
            items.append((y0, text))
    lines = []
    for _, text in sorted(items, key=lambda i: i[0]):
        for line in text.splitlines():
            line = re.sub(r"[ \t]+", " ", line).strip()
            if line and not any(p.match(line) for p in _HEADER_LINES):
                lines.append(line)
    return lines


def _merge_split_tables(lines: list[str]) -> list[str]:
    """A table continued on the next page repeats its header; drop the repeated header rows."""
    out, headers = [], set()
    i = 0
    while i < len(lines):
        line = lines[i]
        is_header = line.startswith("|") and i + 1 < len(lines) and lines[i + 1].startswith("|---")
        if is_header and line in headers and out and out[-1].startswith("|"):
            i += 2
            continue
        if is_header:
            headers.add(line)
        out.append(line)
        i += 1
    return out


def _pdf_chunks(doc_meta) -> list[dict]:
    image_only_pages = {i.page for i in doc_meta.image_only}
    pdf = pymupdf.open(ROOT / doc_meta.file)
    sections: list[dict] = []
    current = {"section": "Introduction", "page": 1, "lines": []}
    for page_no, page in enumerate(pdf, 1):
        for line in _page_lines(page):
            m, a = _SECTION_RE.match(line), _ANNEX_RE.match(line)
            if m or a:
                sections.append(current)
                label = f"§{m.group(1)} {m.group(2)}" if m else f"{a.group(1)}: {a.group(2)}"
                current = {"section": label, "page": page_no, "lines": [], "annex": bool(a)}
            else:
                current["lines"].append(line)
    sections.append(current)

    chunks = []
    for s in sections:
        if not s["lines"]:
            continue
        # Image-only annexures are replaced by the verbatim transcription in annexures.md.
        if s.get("annex") and s["page"] in image_only_pages:
            continue
        if "frequently asked questions" in s["section"].lower():
            chunks += _faq_chunks(doc_meta, s)
            continue
        chunks.append(_chunk(doc_meta, s["section"], s["page"], "\n".join(_merge_split_tables(s["lines"]))))
    return chunks


def _faq_chunks(doc_meta, section: dict) -> list[dict]:
    out, current = [], []
    for line in section["lines"]:
        if _FAQ_Q_RE.match(line) and current:
            out.append(current)
            current = []
        current.append(line)
    if current:
        out.append(current)
    number = section["section"].split()[0]                    # "§5"
    return [_chunk(doc_meta, f"{number} FAQ: {qa[0][3:].strip()}", section["page"], "\n".join(qa))
            for qa in out]


def _chunk(doc_meta, section: str, page: int, body: str, transcribed: bool = False) -> dict:
    header = f"{doc_meta.doc_id} · {doc_meta.title} (version {doc_meta.version})"
    if doc_meta.applies_to:
        header += f" · Applies to: {doc_meta.applies_to}"
    header += f" · {section}"
    label = f"{doc_meta.doc_id} {section}, p.{page}"
    if transcribed:
        label += " (transcribed from scanned image)"
    return {"doc_id": doc_meta.doc_id, "title": doc_meta.title, "version": doc_meta.version,
            "applies_to": doc_meta.applies_to, "section": section, "page": page,
            "source_label": label, "header": header, "text": body, "transcribed": transcribed}


# ------------------------------------------------------------------ annexures.md → chunks

def _annexure_chunks(documents) -> list[dict]:
    chunks = []
    for block in re.split(r"^## ", ANNEXURES.read_text(encoding="utf-8"), flags=re.M)[1:]:
        heading, _, body = block.partition("\n")
        doc_id = re.search(r"RW-POL-\d+", heading).group()
        page = int(re.search(r"page (\d+)", body).group(1))
        section = heading.split("—", 1)[1].strip()
        body = re.sub(r'<a id="[^"]+"></a>', "", body).strip()
        chunks.append(_chunk(documents.get(doc_id), section, page, body, transcribed=True))
    return chunks


def build_chunks() -> list[dict]:
    documents = load_rules().documents
    chunks = []
    for d in documents.documents:
        chunks += _pdf_chunks(d)
    return chunks + _annexure_chunks(documents)


# ------------------------------------------------------------------ manifest (freshness)

def _manifest() -> dict:
    documents = load_rules().documents
    files = [ROOT / d.file for d in documents.documents] + [ANNEXURES, settings.rules_dir / "documents.yaml"]
    return {"ingest_version": INGEST_VERSION, "embedding_model": settings.embedding_model,
            "files": {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}


def _manifest_path() -> Path:
    return settings.index_path / "manifest.json"


def index_is_fresh() -> bool:
    p = _manifest_path()
    return p.exists() and json.loads(p.read_text()) == _manifest()


# ------------------------------------------------------------------ build

def build_index(verbose: bool = False) -> int:
    from qdrant_client import models

    from ridewise.retrieval import dense_model, get_client, sparse_model

    chunks = build_chunks()
    texts = [f"{c['header']}\n{c['text']}" for c in chunks]
    dense = list(dense_model().passage_embed(texts))
    sparse = list(sparse_model().passage_embed(texts))

    client = get_client()
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION,
        vectors_config={"dense": models.VectorParams(size=len(dense[0]), distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    client.upsert(COLLECTION, points=[
        models.PointStruct(
            id=i,
            vector={"dense": d.tolist(),
                    "bm25": models.SparseVector(indices=s.indices.tolist(), values=s.values.tolist())},
            payload=c,
        )
        for i, (c, d, s) in enumerate(zip(chunks, dense, sparse))
    ])
    _manifest_path().write_text(json.dumps(_manifest(), indent=2))
    if verbose:
        for c in chunks:
            print(f"--- [{c['source_label']}] ({len(c['text'])} chars)\n{c['text'][:300]}\n")
    return len(chunks)


def ensure_index() -> None:
    """Build the index if it is missing or any source document changed."""
    if not index_is_fresh():
        build_index()


if __name__ == "__main__":
    settings.index_path.mkdir(parents=True, exist_ok=True)
    n = build_index(verbose="--show" in sys.argv)
    print(f"Indexed {n} chunks into {settings.index_path}/ (collection '{COLLECTION}')")
