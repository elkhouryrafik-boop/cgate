"""
conflictscan — shared-content detection between a proposed submission and
material a named third party supplied to the firm while the bid was prepared.

Why this exists
---------------
A recurring procurement-integrity failure is a firm's submission and its
subcontractor's independent submission containing sections and photographs
that are identical. The firm never holds another bidder's submission, so this
module does not, and cannot, compare two bids. What the firm does hold is the
material its subcontractors and partners supplied during preparation; this
module compares the proposal against that, so third-party origin is visible to
the Compliance Officer before sending. The control against a counterparty
bidding concurrently is the signed declaration (checklist items 6, 7, 9).

It is checklist item 6's `shared_content_detection` mechanism (see
config/checklist.example.yaml) and supports items 7, 9 and 15.

What it does NOT do
-------------------
It does not decide anything. It reports similarity with evidence attached and
returns a set of structural escalation triggers. The Compliance Officer
determines whether a conflict exists. There is no confidence score in the
output and no verdict field, by design.

Design notes
------------
* Text similarity uses word-level shingling with MinHash, plus exact
  normalised-paragraph matching. Exact matches are reported separately from
  estimated overlap because they are qualitatively different evidence.
* Image similarity uses a difference hash (dHash). Two photographs that are
  the same image at different compression, scale or crop-free resize produce
  a small Hamming distance. This is the "identical photographs" detector.
* Access failures return a structured error with `retryable` set. A source we
  could not read is never reported as a source with nothing in it. (CCA P6.)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Iterable

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Thresholds. Tuned for recall, not precision: a missed conflict is a
# serious event, a false flag costs the CO a few minutes.
# ---------------------------------------------------------------------------

SHINGLE_SIZE = 5              # words per shingle
MINHASH_PERMUTATIONS = 256
TEXT_SIMILARITY_TRIGGER = 0.15   # estimated Jaccard overlap
EXACT_PARAGRAPH_MIN_WORDS = 12   # ignore short boilerplate lines
IMAGE_HAMMING_TRIGGER = 10       # out of 64 bits; 0 == pixel-identical after resize
MIN_IMAGE_PIXELS = 10_000        # skip icons, logos, bullets

# Document properties compared across the two files.
#
# `strong` fields name a person, an organisation or a specific file. A shared
# value there is close to conclusive and is reported as its own trigger.
# `weak` fields name software. Two documents made with the same version of Word is
# unremarkable, so those are recorded as evidence for the reviewer to read but
# do not fire a trigger on their own.
METADATA_FIELDS = {
    "author": "strong",
    "last_modified_by": "strong",
    "company": "strong",
    "title": "strong",
    "subject": "weak",
    "keywords": "weak",
    "creator": "weak",
    "producer": "weak",
}

# Values that mean "unset" in one toolchain or another and must never match.
METADATA_NULLS = {"", "unknown", "none", "n/a", "untitled", "user", "windows user",
                  "microsoft office user", "administrator", "owner", "pc", "admin"}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class SourceError:
    """An access failure. Distinct from 'read successfully, found nothing'."""
    path: str
    error_type: str
    message: str
    retryable: bool

@dataclass
class ExactMatch:
    words: int
    text: str
    doc_a_index: int
    doc_b_index: int

@dataclass
class ImageMatch:
    hamming_distance: int
    doc_a_ref: str
    doc_b_ref: str
    doc_a_sha256: str
    doc_b_sha256: str
    identical_bytes: bool

@dataclass
class MetadataMatch:
    """A document property both files carry identically.

    A proposal section built from material a third party supplied keeps that
    material's author, template or producer string. A shared value is the
    cheapest strong signal in the scanner that one document was made from the
    other, and it points the Compliance Officer at exactly where third-party
    origin needs declaring.
    """
    field: str
    value: str
    weight: str          # strong | weak, see METADATA_FIELDS


@dataclass
class ScanResult:
    doc_a: str
    doc_b: str
    text_similarity: float
    exact_matches: list = field(default_factory=list)
    image_matches: list = field(default_factory=list)
    metadata_matches: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    triggers: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def extract(path: str) -> tuple[list[str], list[tuple[str, bytes]], list[SourceError]]:
    """Return (paragraphs, [(ref, image_bytes)], errors) for a PDF or DOCX."""
    errors: list[SourceError] = []
    if not os.path.exists(path):
        return [], [], [SourceError(path, "not_found", "file does not exist", False)]

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(path) + (errors,)
        if ext == ".docx":
            return _extract_docx(path) + (errors,)
        return [], [], [SourceError(path, "unsupported_type", f"cannot read '{ext}'", False)]
    except Exception as exc:                                   # noqa: BLE001
        # Access/parse failure -> structured error, never a silent empty result.
        return [], [], [SourceError(path, "extraction_failed", str(exc), True)]


def _extract_pdf(path: str):
    import pymupdf

    paragraphs: list[str] = []
    images: list[tuple[str, bytes]] = []
    doc = pymupdf.open(path)
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            for block in page.get_text("blocks"):
                txt = _normalise(block[4])
                if txt:
                    paragraphs.append(txt)
            for idx, info in enumerate(page.get_images(full=True)):
                xref = info[0]
                try:
                    raw = doc.extract_image(xref)
                except Exception:                              # noqa: BLE001
                    continue
                images.append((f"p{pno + 1}#img{idx + 1}", raw["image"]))
    finally:
        doc.close()
    return paragraphs, images


def _extract_docx(path: str):
    import zipfile
    from docx import Document

    paragraphs = [
        _normalise(p.text) for p in Document(path).paragraphs if p.text.strip()
    ]
    images: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.startswith("word/media/"):
                images.append((name.split("/")[-1], zf.read(name)))
    return paragraphs, images


# ---------------------------------------------------------------------------
# Text similarity
# ---------------------------------------------------------------------------

def _shingles(paragraphs: Iterable[str]) -> set[int]:
    words = " ".join(paragraphs).split()
    if len(words) < SHINGLE_SIZE:
        return set()
    return {
        hash(" ".join(words[i:i + SHINGLE_SIZE]))
        for i in range(len(words) - SHINGLE_SIZE + 1)
    }


def _minhash(shingles: set[int], permutations: int = MINHASH_PERMUTATIONS) -> np.ndarray:
    if not shingles:
        return np.full(permutations, np.iinfo(np.uint64).max, dtype=np.uint64)
    arr = np.fromiter(shingles, dtype=np.int64, count=len(shingles)).astype(np.uint64)
    rng = np.random.default_rng(seed=0xC0FFEE)          # fixed: results reproducible
    a = rng.integers(1, 2**61, size=permutations, dtype=np.uint64)
    b = rng.integers(0, 2**61, size=permutations, dtype=np.uint64)
    prime = np.uint64((1 << 61) - 1)
    hashed = (a[:, None] * arr[None, :] + b[:, None]) % prime
    return hashed.min(axis=1)


def _jaccard(sig_a: np.ndarray, sig_b: np.ndarray) -> float:
    return float(np.mean(sig_a == sig_b))


def _exact_matches(pa: list[str], pb: list[str]) -> list[ExactMatch]:
    index: dict[str, int] = {}
    for i, para in enumerate(pa):
        if len(para.split()) >= EXACT_PARAGRAPH_MIN_WORDS:
            index.setdefault(para, i)
    out: list[ExactMatch] = []
    for j, para in enumerate(pb):
        if len(para.split()) >= EXACT_PARAGRAPH_MIN_WORDS and para in index:
            out.append(ExactMatch(len(para.split()), para[:300], index[para], j))
    return out


# ---------------------------------------------------------------------------
# Image similarity — difference hash
# ---------------------------------------------------------------------------

def _dhash(data: bytes) -> tuple[int, int] | None:
    """Return (64-bit dhash, pixel_count) or None if unreadable/too small."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:                                          # noqa: BLE001
        return None
    if img.width * img.height < MIN_IMAGE_PIXELS:
        return None
    small = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = np.asarray(small, dtype=np.int16)
    bits = px[:, 1:] > px[:, :-1]
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bit)
    return value, img.width * img.height


def _hamming(x: int, y: int) -> int:
    return bin(x ^ y).count("1")


def _image_matches(ia, ib) -> list[ImageMatch]:
    hashed_a = []
    for ref, data in ia:
        h = _dhash(data)
        if h:
            hashed_a.append((ref, data, h[0]))
    hashed_b = []
    for ref, data in ib:
        h = _dhash(data)
        if h:
            hashed_b.append((ref, data, h[0]))

    out: list[ImageMatch] = []
    for ref_a, data_a, ha in hashed_a:
        for ref_b, data_b, hb in hashed_b:
            dist = _hamming(ha, hb)
            if dist <= IMAGE_HAMMING_TRIGGER:
                sa = hashlib.sha256(data_a).hexdigest()
                sb = hashlib.sha256(data_b).hexdigest()
                out.append(ImageMatch(dist, ref_a, ref_b, sa, sb, sa == sb))
    return out


# ---------------------------------------------------------------------------
# Document metadata
# ---------------------------------------------------------------------------

def _clean_meta(value) -> str:
    """Normalise a property value, or return "" if it carries no information."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.lower() in METADATA_NULLS:
        return ""
    return text


def read_metadata(path: str) -> tuple[dict, list[SourceError]]:
    """Document properties, normalised to one vocabulary across PDF and DOCX.

    An unreadable file returns a structured error, never an empty dict. Two
    documents that could not be read must not appear to share every property
    by both having none.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import fitz
            with fitz.open(path) as doc:
                raw = doc.metadata or {}
            meta = {
                "author": _clean_meta(raw.get("author")),
                "title": _clean_meta(raw.get("title")),
                "subject": _clean_meta(raw.get("subject")),
                "keywords": _clean_meta(raw.get("keywords")),
                "creator": _clean_meta(raw.get("creator")),
                "producer": _clean_meta(raw.get("producer")),
                "last_modified_by": "",
                "company": "",
            }
        elif ext == ".docx":
            import docx
            cp = docx.Document(path).core_properties
            meta = {
                "author": _clean_meta(cp.author),
                "title": _clean_meta(cp.title),
                "subject": _clean_meta(cp.subject),
                "keywords": _clean_meta(cp.keywords),
                "creator": "",
                "producer": "",
                "last_modified_by": _clean_meta(cp.last_modified_by),
                "company": _clean_meta(getattr(cp, "category", None)),
            }
        else:
            return {}, [SourceError(path, "unsupported_filetype",
                                    f"cannot read document properties from {ext or 'a file with no extension'}",
                                    retryable=False)]
    except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
        return {}, [SourceError(path, "metadata_unreadable", str(exc), retryable=True)]
    return meta, []


def _metadata_matches(ma: dict, mb: dict) -> list[MetadataMatch]:
    out = []
    for field_name, weight in METADATA_FIELDS.items():
        a, b = ma.get(field_name, ""), mb.get(field_name, "")
        if a and b and a.casefold() == b.casefold():
            out.append(MetadataMatch(field=field_name, value=a, weight=weight))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def scan(path_a: str, path_b: str) -> ScanResult:
    """Compare a proposed submission against third-party material.

    Returns evidence and structural triggers. Never returns a verdict.
    """
    pa, ia, err_a = extract(path_a)
    pb, ib, err_b = extract(path_b)

    result = ScanResult(doc_a=path_a, doc_b=path_b, text_similarity=0.0)
    result.errors = [asdict(e) for e in (*err_a, *err_b)]

    if result.errors:
        # A source we could not read is not a source with nothing in it.
        result.triggers.append("source_unreadable")
        return result

    sim = _jaccard(_minhash(_shingles(pa)), _minhash(_shingles(pb)))
    result.text_similarity = round(sim, 4)
    result.exact_matches = [asdict(m) for m in _exact_matches(pa, pb)]
    result.image_matches = [asdict(m) for m in _image_matches(ia, ib)]

    ma, err_ma = read_metadata(path_a)
    mb, err_mb = read_metadata(path_b)
    result.errors += [asdict(e) for e in (*err_ma, *err_mb)]
    result.metadata = {"doc_a": ma, "doc_b": mb}
    result.metadata_matches = [asdict(m) for m in _metadata_matches(ma, mb)]

    result.counts = {
        "paragraphs_a": len(pa), "paragraphs_b": len(pb),
        "images_a": len(ia), "images_b": len(ib),
    }

    if sim >= TEXT_SIMILARITY_TRIGGER:
        result.triggers.append("content_similarity_above_threshold")
    if result.exact_matches:
        result.triggers.append("exact_passage_match")
    if result.image_matches:
        result.triggers.append("image_match_found")
    if any(m["identical_bytes"] for m in result.image_matches):
        result.triggers.append("byte_identical_image")
    if any(m["weight"] == "strong" for m in result.metadata_matches):
        result.triggers.append("metadata_origin_match")

    return result


if __name__ == "__main__":
    # Matched passages carry the source document's own characters; the Windows
    # console default (cp1252) cannot encode all of them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 3:
        print("usage: conflictscan.py <submission> <third_party_material>")
        raise SystemExit(2)
    print(scan(sys.argv[1], sys.argv[2]).to_json())
