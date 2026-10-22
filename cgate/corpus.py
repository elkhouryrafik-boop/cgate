"""
cgate.corpus — the instruments that bind us, held locally and under hash.

A tender that says "the General Conditions apply" and does not attach them
leaves the review with a hole in it, and `resolve.py` is right to report that
hole rather than assume its content. The corpus is how the hole gets closed:
the standard instruments, fetched once, hashed, and dated, so that the next
review can say which document the tender means and a diff can say what the
client changed in it.

Three rules, and they are the whole design.

**A file without provenance is not evidence.** Every fetch records the URL
asked for, the URL that answered after redirects, the HTTP status, the byte
count, the content type, the SHA-256 and the UTC timestamp. A document that
cannot be traced back to a publisher and a moment is a document nobody should
rely on in a submission somebody signs.

**The corpus is a cache, not a library.** Fetched files are gitignored. The
publishers here serve their documents without authentication, which is not the
same as licensing us to redistribute them, and a repository is redistribution.
What is committed is `sources.example.yaml` (copy it to `sources.yaml` and edit)
and, once you have fetched, `manifest.json` — the record of what was fetched,
from where, and what it hashed to. Those are ours.

**Instruments move.** `verify --remote` re-fetches every source and compares
the hash. This is not housekeeping. ISO 37001:2016 was superseded in February
2025, and a document set that cites the old edition can go unnoticed for a long time. A corpus nobody re-checks is worse than no corpus,
because it looks current.

What this module deliberately does not do: it does not decide that a fetched
instrument is "the" instrument a tender meant. It offers a candidate, by name,
with its hash, and a human supplies it to the review. Matching a title is not
the same as reading the document, and `resolve.py` keeps that distinction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

import yaml

# Publishers serve these documents to browsers. Several return 403 to a client
# that declines to identify itself, so the fetcher identifies itself as what it
# is and accepts the consequences of being blocked rather than pretending to be
# something else.
USER_AGENT = (
    "cgate-corpus/0.1 (compliance reference fetcher; "
    "+https://example.invalid/cgate) python-urllib"
)
TIMEOUT = 90
CHUNK = 1 << 16

EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}


def corpus_root(root: str | None = None) -> str:
    if root:
        return root
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "corpus")


# ---------------------------------------------------------------------------


@dataclass
class Entry:
    """One fetched instrument, with everything needed to reproduce the fetch."""

    id: str
    title: str
    issuer: str
    category: str
    url: str
    licence: str
    edition_hint: str = ""
    # Set by the fetch
    final_url: str = ""
    http_status: int = 0
    content_type: str = ""
    bytes: int = 0
    sha256: str = ""
    retrieved_at: str = ""
    filename: str = ""
    # What the document says about its own edition, read off its first page.
    # The point of recording it is the case where it disagrees with
    # `edition_hint` — that disagreement is the finding.
    declared: str = ""
    error: str = ""

    def ok(self) -> bool:
        return bool(self.sha256) and not self.error


# ---------------------------------------------------------------------------


def load_sources(root: str | None = None) -> tuple[list[dict], list[dict]]:
    """(sources, unavailable) from sources.yaml."""
    path = os.path.join(corpus_root(root), "sources.yaml")
    if not os.path.exists(path):
        path = os.path.join(corpus_root(root), "sources.example.yaml")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("sources", []) or [], data.get("unavailable", []) or []


def load_manifest(root: str | None = None) -> list[Entry]:
    path = os.path.join(corpus_root(root), "manifest.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [Entry(**e) for e in data.get("entries", [])]


def save_manifest(entries: list[Entry], root: str | None = None) -> str:
    path = os.path.join(corpus_root(root), "manifest.json")
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len([e for e in entries if e.ok()]),
        "failed": len([e for e in entries if not e.ok()]),
        "entries": [asdict(e) for e in entries],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------


def _first_page_text(path: str) -> str:
    """What the document says about itself on page one.

    Publishers put the edition in the running head — "UN Supplier Code of
    Conduct Rev.07 – September 2024", "VERSION DATE: 10 March 2025". Recording
    it means a later fetch that silently swaps editions is visible in the
    manifest diff rather than only in the hash.
    """
    try:
        if path.lower().endswith(".pdf"):
            import fitz

            with fitz.open(path) as doc:
                if not doc.page_count:
                    return ""
                text = doc.load_page(0).get_text("text")
        else:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read(20000)
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                          flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
    except Exception:
        return ""
    return " ".join(text.split())[:400]


def fetch_one(spec: dict, root: str | None = None) -> Entry:
    """Fetch one source. Never raises — a failed fetch is a recorded failure."""
    e = Entry(
        id=spec["id"],
        title=spec.get("title", ""),
        issuer=spec.get("issuer", ""),
        category=spec.get("category", ""),
        url=spec["url"],
        licence=spec.get("licence", "unknown"),
        edition_hint=str(spec.get("edition_hint", "")),
    )
    files = os.path.join(corpus_root(root), "files")
    os.makedirs(files, exist_ok=True)

    req = urllib.request.Request(e.url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })
    tmp = os.path.join(files, f".{e.id}.part")
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            e.http_status = resp.status
            e.final_url = resp.geturl()
            e.content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            with open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    n += len(chunk)
    except urllib.error.HTTPError as exc:
        e.http_status = exc.code
        e.error = f"http_{exc.code}"
    except Exception as exc:                       # network, TLS, timeout
        e.error = f"{type(exc).__name__}: {exc}"

    if e.error:
        if os.path.exists(tmp):
            os.remove(tmp)
        return e

    ext = EXTENSIONS.get(e.content_type) or os.path.splitext(e.url)[1][:6] or ".bin"
    e.filename = f"{e.id}{ext.lower()}"
    e.bytes = n
    e.sha256 = h.hexdigest()
    final = os.path.join(files, e.filename)
    os.replace(tmp, final)
    e.declared = _first_page_text(final)
    e.retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # A publisher that serves an error page with status 200 is common enough
    # that byte count is worth a look. Ten kilobytes is not a procurement
    # manual; recording the suspicion is cheaper than discovering it later.
    if n < 10_000 and e.content_type == "application/pdf":
        e.error = "suspiciously_small"
    return e


def fetch_all(only: list[str] | None = None, root: str | None = None,
              progress=None) -> list[Entry]:
    sources, _ = load_sources(root)
    if only:
        wanted = set(only)
        sources = [s for s in sources if s["id"] in wanted]
    out: list[Entry] = []
    for s in sources:
        e = fetch_one(s, root)
        out.append(e)
        if progress:
            progress(e)
    return out


# ---------------------------------------------------------------------------


def _same_declaration(a: str, b: str) -> bool:
    """Do two first pages say the same thing about what the document is?"""
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

    return norm(a) == norm(b) and bool(norm(a))


@dataclass
class VerifyResult:
    checked: int = 0
    unchanged: int = 0
    missing_local: list[str] = field(default_factory=list)
    corrupt_local: list[str] = field(default_factory=list)
    changed_upstream: list[dict] = field(default_factory=list)
    unreachable: list[dict] = field(default_factory=list)


def verify(remote: bool = False, root: str | None = None,
           progress=None) -> VerifyResult:
    """Re-hash what is on disk, and optionally re-fetch to see if it moved.

    The local check answers "is the file I have the file I recorded". The
    remote check answers the question that actually costs money: "is the
    document I am relying on still the document the publisher is serving".
    """
    res = VerifyResult()
    entries = load_manifest(root)
    files = os.path.join(corpus_root(root), "files")
    sources = {s["id"]: s for s in load_sources(root)[0]}

    for e in entries:
        if not e.ok():
            continue
        res.checked += 1
        path = os.path.join(files, e.filename)
        if not os.path.exists(path):
            res.missing_local.append(e.id)
            continue
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                h.update(chunk)
        if h.hexdigest() != e.sha256:
            res.corrupt_local.append(e.id)
            continue

        if not remote:
            res.unchanged += 1
            continue

        spec = sources.get(e.id)
        if spec is None:
            continue
        fresh = fetch_one(spec, root)
        if progress:
            progress(fresh)
        if fresh.error:
            res.unreachable.append({"id": e.id, "error": fresh.error})
        elif fresh.sha256 != e.sha256:
            # Different bytes are not the same thing as a different document.
            # Publishers re-save PDFs; the timestamp in the file metadata moves
            # and the hash with it, while the policy number, version and
            # effective date on page one do not. A check that shouts "changed"
            # at every re-render is a check people stop reading, so the first
            # page decides which of the two happened.
            same_text = _same_declaration(e.declared, fresh.declared)
            res.changed_upstream.append({
                "id": e.id,
                "title": e.title,
                "was": e.sha256,
                "now": fresh.sha256,
                "was_retrieved": e.retrieved_at,
                "declared_was": e.declared[:160],
                "declared_now": fresh.declared[:160],
                "kind": "reissued_bytes" if same_text else "edition_changed",
            })
        else:
            res.unchanged += 1
    return res


# ---------------------------------------------------------------------------


# Words that every instrument of this kind contains. They are kept, because
# "General Conditions" is mostly made of them and is exactly the string a
# tender uses — but they cannot identify an issuer, which is why a match built
# only from them returns every candidate rather than a winner.
_GENERIC = {"general", "terms", "conditions", "condition", "contract",
            "contracts", "provision", "services", "goods", "annex", "policy"}

ISSUERS = {
    "exemplar": "Exemplar", "sampleton": "Sampleton",
    "fidic": "FIDIC", "european union": "European Union",
}


# Function words are long enough to survive a three-character filter and carry
# no identity at all. Left in, "and" alone was enough to pair an agency's
# anti-fraud policy with that agency's terms and conditions.
_FUNCTION = {"and", "the", "for", "of", "with", "its", "any", "all", "from",
             "this", "that", "under", "such", "into", "per", "are", "was"}


def _tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", name.lower())
            if w not in _FUNCTION}


def issuer_in(text: str) -> str | None:
    """The publisher a piece of text names, if it names exactly one."""
    low = text.lower()
    found = {v for k, v in ISSUERS.items() if k in low}
    return found.pop() if len(found) == 1 else None


def candidates(instrument: str, issuer_hint: str | None = None,
               root: str | None = None, limit: int = 3) -> list[Entry]:
    """Corpus entries that might be the instrument named, best first.

    Returns a list on purpose. "General Conditions" identifies a shape of
    document, not a document, and a function that picked one anyway would be
    inventing the issuer. The caller reports candidates; a human supplies the
    file. Matching a title is not reading it.
    """
    want = _tokens(instrument)
    if not want:
        return []
    named = issuer_in(instrument) or issuer_hint

    scored: list[tuple[float, Entry]] = []
    for e in load_manifest(root):
        if not e.ok():
            continue
        if named and named.lower() not in e.issuer.lower():
            continue
        have = _tokens(f"{e.issuer} {e.title}")
        overlap = want & have
        if len(overlap) < 2:
            continue
        # A match made entirely of words every such instrument carries is a
        # match on the category, not on the document.
        distinctive = overlap - _GENERIC
        score = len(overlap) / len(want | have) + (0.5 if distinctive else 0.0)
        # Prefer a live edition over one the publisher has archived.
        if "archive" in (e.edition_hint or "").lower():
            score -= 0.2
        scored.append((score, e))

    scored.sort(key=lambda t: (-t[0], t[1].id))
    return [e for _, e in scored[:limit]]


def lookup(instrument: str, issuer_hint: str | None = None,
           root: str | None = None) -> Entry | None:
    """The single unambiguous candidate, or None. Never guesses between two."""
    found = candidates(instrument, issuer_hint, root, limit=2)
    return found[0] if len(found) == 1 else None
