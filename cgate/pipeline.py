"""
cgate.pipeline — the review, as one callable.

`cgate review` on a terminal and the Word add-in must not be two implementations
of the same procedure. A review signed off the command line and a review read in
the margin of the document have to be the same review, or the document hash
printed on both is a lie. So the pipeline lives here, once, and both callers are
thin.
"""

from __future__ import annotations

import os
import time

from . import TOOL_VERSION
from . import feedback as fb
from .adjudicate import adjudicate, load_bundle, load_checklist, summarise
from .extract import extract
from .ground import gate
from .ingest import ingest
from .lint import lint
from .model import ReviewResult
from .resolve import resolve_incorporation
from .segment import segment
from .spec import compile_spec


def run_review(
    paths: list[str],
    checklist_path: str,
    bundle_path: str | None = None,
    store: str = ".cgate",
    bundle_data: dict | None = None,
) -> tuple[ReviewResult, str, list[dict]]:
    """Run the full review.

    Returns (result, lexicon_version, blocking_errors). Raises whatever
    `load_checklist` raises — a checklist this build cannot honour must stop the
    run at both call sites identically.

    `bundle_data` is evidence already in memory, as assembled by `cgate.gate`
    from the register. It takes precedence over `bundle_path`, so a caller that
    has gathered evidence itself does not have to write it to a file and read it
    back in order to be adjudicated by the same code as a hand-written bundle.
    """
    t0 = time.time()

    # Approved lexicon overrides are folded in before anything is read, and the
    # version is stamped on the report, so the run is reproducible.
    lexicon_version = fb.apply_overrides(store)
    checklist = load_checklist(checklist_path)

    # A solicitation and the annexes it incorporates are one review, not several.
    # The first file is the primary document; the rest are read so that clauses
    # incorporated by reference can be resolved against what is actually in hand.
    docs = [ingest(p) for p in paths]
    doc = docs[0]
    blocking = [e for e in doc.errors if e.get("blocking")]

    clauses_by_doc = [(d, segment(d) if d.blocks else []) for d in docs]
    resolution_notes = resolve_incorporation(docs, clauses_by_doc)

    kept: list = []
    n_clauses = n_extracted = n_dropped = 0
    drop_reasons: dict[str, int] = {}
    for i, (d, clauses) in enumerate(clauses_by_doc):
        reqs = extract(d, clauses) if clauses else []
        if len(docs) > 1:
            for r in reqs:
                r.req_id = f"d{i + 1}-{r.req_id}"
        doc_kept, rep = gate(d, reqs)
        kept.extend(doc_kept)
        n_clauses += len(clauses)
        n_extracted += len(reqs)
        n_dropped += rep.dropped
        for reason, count in rep.reasons.items():
            drop_reasons[reason] = drop_reasons.get(reason, 0) + count

    ingest_notes = list(doc.notes) + resolution_notes
    for d in docs[1:]:
        ingest_notes += [f"{os.path.basename(d.path)}: {n}" for n in d.notes]

    bundle = bundle_data if bundle_data is not None else load_bundle(bundle_path)
    items = adjudicate(checklist, doc, kept, bundle)

    # The compiled specification runs over the primary document only. It reads
    # the solicitation as a whole rather than through the integrity lexicon, and
    # what it finds is a property of the client's document, not evidence about
    # the firm — so it never touches a checklist status.
    spec = lint(doc, compile_spec(doc)) if doc.blocks else None

    errors: list[dict] = []
    for d in docs:
        errors += [{**e, "document": os.path.basename(d.path)} for e in d.errors]

    result = ReviewResult(
        doc_id=doc.doc_id,
        doc_path=os.path.abspath(paths[0]),
        checklist_version=str(checklist.get("version", "?")),
        tool_version=TOOL_VERSION,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        requirements=kept,
        items=items,
        ingest_notes=ingest_notes,
        errors=errors,
        stats={
            "documents": [
                {"path": os.path.basename(d.path), "doc_id": d.doc_id,
                 "pages": d.n_pages, "blocks": len(d.blocks)}
                for d in docs
            ],
            "pages": sum(d.n_pages for d in docs),
            "blocks": sum(len(d.blocks) for d in docs),
            "clauses": n_clauses,
            "requirements_extracted": n_extracted,
            "requirements_kept": len(kept),
            "requirements_dropped": n_dropped,
            "drop_rate": round(n_dropped / n_extracted, 4) if n_extracted else 0.0,
            "drop_reasons": drop_reasons,
            "lexicon_version": lexicon_version,
            "defects": len(spec.defects) if spec else 0,
            "elapsed_seconds": round(time.time() - t0, 2),
            **summarise(items),
        },
    )
    if spec is not None:
        result.defects = spec.defects
        result.coverage = {
            "blocks_total": spec.blocks_total,
            "blocks_compiled": spec.blocks_compiled,
            "blocks_uncompiled": len(spec.uncompiled),
            "commitments": len(spec.commitments),
            "measurands": len(spec.measurands),
            "uncompiled": spec.uncompiled,
            "notes": spec.notes,
        }
    return result, lexicon_version, blocking
