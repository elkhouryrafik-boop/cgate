"""
cgate.serve — the local host the Word add-in talks to.

Where this runs, and why it matters
-----------------------------------
On the machine the document is already open on. Bound to loopback, never to an
interface. The task pane is served from the same origin as the API, so nothing
is cross-origin and no document is ever transmitted anywhere. Whether these
files may go to a cloud service is a common procurement question; this server
is the architecture that makes the answer "they never leave the machine" a fact
about the code rather than a promise in a document.

TLS is not optional
-------------------
Office refuses to load a task pane over plain HTTP. `cgate.certs` issues a
self-signed certificate valid for localhost and nothing else. `--insecure`
exists for testing the pane in a browser and is refused a hostname other than
loopback.

What it does not do
-------------------
No authentication, because there is no remote caller to authenticate: the socket
is not reachable from off the machine. No database — reviews are written to disk
as the same `review.json` and `review.md` the command line produces, so a review
read in Word and a review read in a terminal are the same artifact with the same
hash. Nothing is retained in memory that is not also on disk.
"""

import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

from .adjudicate import ChecklistError, load_checklist
from .model import ReviewResult
from .pipeline import run_review
from .report import STATUS_LABEL, STATUS_MARK, render as render_report
from .datarequest import render as render_request
from . import anchor as anchor_
from . import feedback as fb
from . import reading

# A solicitation package is large; a 200 MB upload on loopback is not an attack,
# it is a tender with drawings. The cap exists to fail clearly rather than to
# exhaust memory silently.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

ADDIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addin")

# Extensions `ingest` can actually read. Anything else is refused at the door
# with the reason, rather than failing three stages later.
ACCEPTED = {".pdf", ".docx", ".txt", ".md"}


def _payload(
    result: ReviewResult,
    lexicon_version: str,
    blocking: List[dict],
    checklist: Dict[str, Any],
    store: str,
    artifacts: Dict[str, str],
) -> Dict[str, Any]:
    """The review, shaped for a reader in the margin of the document."""
    item_status = {i.item_id: i.status for i in result.items}
    calib = fb.calibration(fb.load_ledger(store))

    counts: dict[str, int] = {}
    for it in result.items:
        counts[it.status.value] = counts.get(it.status.value, 0) + 1

    return {
        "doc_id": result.doc_id,
        "doc_path": result.doc_path,
        "doc_name": os.path.basename(result.doc_path),
        "generated_at": result.generated_at,
        "tool_version": result.tool_version,
        "checklist_version": result.checklist_version,
        "lexicon_version": lexicon_version,
        "stats": result.stats,
        "counts": counts,
        "blocking": blocking,
        "items": [
            {
                "item_id": i.item_id,
                "section": i.section,
                "text": i.text,
                "handling": i.handling,
                "status": i.status.value,
                "status_label": STATUS_LABEL[i.status],
                "status_mark": STATUS_MARK[i.status],
                "triggers": i.triggers,
                "notes": i.notes,
                "requirements": i.requirements,
                "never_auto_clear": i.never_auto_clear,
            }
            for i in result.items
        ],
        "provisions": [reading.read(r, item_status) for r in result.requirements],
        "ingest_notes": result.ingest_notes,
        "errors": result.errors,
        "calibration": {
            cid: {
                "confirmed": c.confirmed,
                "rejected": c.rejected,
                "missed": c.missed,
                "precision": c.precision,
                "recall": c.recall,
            }
            for cid, c in (calib or {}).items()
        },
        "artifacts": artifacts,
    }


def _requirements_from_review(review_path: str) -> list:
    """Rebuild Requirement objects from a stored review.json.

    Only the fields `cgate.submission.deliverables` and `check_coverage` read
    are reconstructed. Storing whole objects would couple the on-disk format to
    the dataclass; this keeps the coupling to five named fields.
    """
    from .model import Actor, Modality, Requirement

    with open(review_path, encoding="utf-8") as fh:
        stored = json.load(fh)
    out = []
    for r in stored.get("requirements", []):
        try:
            modality = Modality(r.get("modality", "unknown"))
            actor = Actor(r.get("actor", "unknown"))
        except ValueError:
            continue
        out.append(Requirement(
            req_id=r.get("req_id", ""),
            category=r.get("category", ""),
            modality=modality,
            actor=actor,
            text=r.get("text", ""),
            spans=[],
            clause_id=r.get("clause_id"),
            clause_number=r.get("clause_number"),
            page=int(r.get("page") or 0),
        ))
    return out


def build_app(checklist_path: str, store: str, work: str):
    from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="cgate — pre-submission compliance review",
        description="Local review host for the Word add-in. Loopback only.",
        docs_url=None,
        redoc_url=None,
    )

    os.makedirs(work, exist_ok=True)

    # The checklist is validated once at start-up rather than per request: a
    # checklist this build cannot honour should stop the server coming up, not
    # produce a run that looks fine until somebody reads it.
    load_checklist(checklist_path)

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        from . import TOOL_VERSION

        return {
            "ok": True,
            "tool_version": TOOL_VERSION,
            "checklist": os.path.abspath(checklist_path),
            "work": os.path.abspath(work),
        }

    @app.post("/api/review")
    async def review(
        files: List[UploadFile] = File(...),
        bundle: Optional[UploadFile] = File(None),
        name: str = Form(""),
    ):
        """Review one solicitation and any annexes it incorporates.

        The first file is the primary document. `name` lets the add-in supply the
        document's real name, which Office does not always expose to the pane.
        """
        if not files:
            raise HTTPException(400, "no document supplied")

        tmp = tempfile.mkdtemp(prefix="cgate-")
        try:
            paths: List[str] = []
            for i, up in enumerate(files):
                base = os.path.basename(up.filename or f"document-{i}")
                if i == 0 and name:
                    base = os.path.basename(name)
                ext = os.path.splitext(base)[1].lower()
                if ext not in ACCEPTED:
                    raise HTTPException(
                        415,
                        f"{base}: cgate reads {', '.join(sorted(ACCEPTED))}. "
                        "A scanned image needs OCR before it can be reviewed, and "
                        "guessing at its text is worse than refusing it.",
                    )
                data = await up.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"{base}: larger than {MAX_UPLOAD_BYTES // (1 << 20)} MB")
                dest = os.path.join(tmp, base)
                with open(dest, "wb") as fh:
                    fh.write(data)
                paths.append(dest)

            bundle_path = None
            if bundle is not None:
                bundle_path = os.path.join(tmp, "evidence.json")
                raw = await bundle.read()
                try:
                    json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HTTPException(400, f"evidence bundle is not valid JSON: {exc}")
                with open(bundle_path, "wb") as fh:
                    fh.write(raw)

            try:
                result, lexicon_version, blocking = run_review(
                    paths, checklist_path, bundle_path, store
                )
            except ChecklistError as exc:
                raise HTTPException(500, f"refusing to run: {exc}")

            # Artifacts are written under the document hash. The same document
            # reviewed twice overwrites its own directory; a changed document
            # gets a new one and can never silently reuse the old review.
            outdir = os.path.join(work, result.doc_id[:16])
            os.makedirs(outdir, exist_ok=True)
            checklist = load_checklist(checklist_path)
            calib = fb.calibration(fb.load_ledger(store))

            artifacts = {
                "json": os.path.join(outdir, "review.json"),
                "md": os.path.join(outdir, "review.md"),
                "request": os.path.join(outdir, "evidence-request.md"),
            }
            payload = _payload(result, lexicon_version, blocking, checklist, store, artifacts)

            # review.json keeps result.to_json()'s shape — cmd_feedback and the
            # /api/feedback handler below both index it by `requirements` — and
            # adds `provisions` (reading.read() output, carrying each one's
            # exposure band) so a later request needs no recomputation to know
            # what band a req_id is in. See cgate/anchor.py §4.1.
            on_disk = json.loads(result.to_json())
            on_disk["provisions"] = payload["provisions"]
            with open(artifacts["json"], "w", encoding="utf-8") as fh:
                json.dump(on_disk, fh, indent=2, ensure_ascii=False)
            with open(artifacts["md"], "w", encoding="utf-8") as fh:
                fh.write(render_report(result, calib or None, lexicon_version))
            with open(artifacts["request"], "w", encoding="utf-8") as fh:
                fh.write(render_request(result, checklist))

            return JSONResponse(payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @app.get("/api/review/{doc_id}/decisions")
    def decisions(doc_id: str):
        """The ledger's decisions for one document, one per req_id (the last
        entry wins — the ledger is append-only, so a later decision on the
        same provision supersedes an earlier one). This is what §6.1
        rehydration reconciles the document's own titles against: the ledger
        is the authority for *decisions*, the document's tags/titles are the
        authority for *anchoring*.

        Registered before the generic `/api/review/{doc_id}/{kind}` file
        route below so `decisions` is never swallowed by that route's `kind`
        path parameter — Starlette matches routes in registration order.
        """
        if not (doc_id.isalnum() and 8 <= len(doc_id) <= 64):
            raise HTTPException(400, "not a document id")
        ledger = fb.load_ledger(store)
        by_req: Dict[str, Dict[str, Any]] = {}
        for row in ledger:
            if row.get("doc_id") == doc_id and row.get("kind") in ("confirm", "reject", "classify") and row.get("req_id"):
                by_req[row["req_id"]] = row
        return {"decisions": list(by_req.values())}

    @app.get("/api/review/{doc_id}/{kind}")
    def download(doc_id: str, kind: str):
        """Serve a written artifact back. `doc_id` is a hash, so it is validated
        as one — a path separator in a URL must never reach the filesystem."""
        if not (doc_id.isalnum() and 8 <= len(doc_id) <= 64):
            raise HTTPException(400, "not a document id")
        names = {"json": "review.json", "md": "review.md", "request": "evidence-request.md"}
        if kind not in names:
            raise HTTPException(404, "no such artifact")
        path = os.path.join(work, doc_id[:16], names[kind])
        if not os.path.exists(path):
            raise HTTPException(404, "not generated for this document")
        media = "application/json" if kind == "json" else "text/markdown"
        return FileResponse(path, media_type=media, filename=names[kind])

    @app.post("/api/feedback")
    async def record_feedback(payload: Dict[str, Any]):
        """Record what the reviewer decided about one extracted provision.

        This is the only write in the system that changes future behaviour, and
        it changes nothing on its own: decisions accumulate in the ledger, and a
        named human has to approve a lexicon change with `cgate learn --approve`
        before any of it affects an extraction.
        """
        kind = payload.get("kind")
        if kind not in ("confirm", "reject", "miss", "classify"):
            raise HTTPException(400, "kind must be confirm, reject, classify or miss")
        reviewer = (payload.get("reviewer") or "").strip()
        if not reviewer:
            raise HTTPException(
                400,
                "a decision needs the name of the person who made it. An anonymous "
                "ledger entry cannot be audited and will not be recorded.",
            )

        doc_id = payload.get("doc_id") or ""
        review_path = os.path.join(work, doc_id[:16], "review.json")
        if not os.path.exists(review_path):
            raise HTTPException(404, "no review on file for that document")
        with open(review_path, encoding="utf-8") as fh:
            review = json.load(fh)
        lexv = str(review.get("stats", {}).get("lexicon_version", "0"))

        if kind == "miss":
            category = payload.get("category")
            quote = (payload.get("quote") or "").strip()
            if not category or not quote:
                raise HTTPException(400, "a reported miss needs a category and the verbatim quote")
            fb.record(store, fb.Decision(
                kind="miss", category=category, reviewer=reviewer, doc_id=doc_id,
                quote=quote, reason=payload.get("reason", ""), lexicon_version=lexv,
            ))
        elif kind == "classify":
            # The third decision (§6.6): a reviewer names the category of a
            # provision the extractor could not classify on its own. Never
            # re-banded client-side — this only reaches the ledger; the next
            # lexicon version, approved by a human, is what would act on it.
            category = payload.get("category")
            if not category:
                raise HTTPException(400, "classify needs the category the reviewer chose (e.g. R4)")
            by_id = {r["req_id"]: r for r in review.get("requirements", [])}
            r = by_id.get(payload.get("req_id"))
            if r is None:
                raise HTTPException(404, "no such provision in this review")
            fb.record(store, fb.Decision(
                kind="classify", category=category, reviewer=reviewer, doc_id=doc_id,
                req_id=r["req_id"], matched=r.get("matched") or [],
                quote=r.get("evidence_quote") or r.get("text", ""),
                reason=payload.get("reason", ""), lexicon_version=lexv,
                source=payload.get("source") or "pane",
            ))
        else:
            by_id = {r["req_id"]: r for r in review.get("requirements", [])}
            r = by_id.get(payload.get("req_id"))
            if r is None:
                raise HTTPException(404, "no such provision in this review")
            fb.record(store, fb.Decision(
                kind=kind, category=r["category"], reviewer=reviewer, doc_id=doc_id,
                req_id=r["req_id"], matched=r.get("matched") or [],
                quote=r.get("evidence_quote") or r.get("text", ""),
                reason=payload.get("reason", ""), lexicon_version=lexv,
                source=payload.get("source") or "pane",
            ))

        calib = fb.calibration(fb.load_ledger(store))
        return {
            "recorded": True,
            "ledger": fb.ledger_path(store),
            "calibration": {
                cid: {"confirmed": c.confirmed, "rejected": c.rejected,
                      "missed": c.missed, "precision": c.precision, "recall": c.recall}
                for cid, c in (calib or {}).items()
            },
        }

    @app.post("/api/feedback/reply")
    async def record_reply(payload: Dict[str, Any]):
        """One Word comment reply, read as a decision (§6.5). Unlike
        `/api/feedback`, a reply that does not parse is not an error — it is
        the ordinary case of a reviewer leaving an ordinary remark — so this
        returns 200 with `parsed: false` and a reason rather than a 4xx, and
        the add-in surfaces that reason as an unread reply (§9.7) instead of
        dropping it.
        """
        doc_id = (payload.get("doc_id") or "").strip()
        reviewer = (payload.get("reviewer") or "").strip()
        comment_content = payload.get("comment_content") or ""
        reply_text = payload.get("reply_text") or ""
        if not doc_id:
            raise HTTPException(400, "doc_id is required")
        if not reviewer:
            raise HTTPException(
                400,
                "a decision needs the name of the person who made it — Word did not "
                "supply an author name for this reply, so it cannot be recorded.",
            )

        req_id = fb.req_id_from_comment(comment_content)
        if not req_id:
            return {"parsed": False, "reason": "could not tell which provision this comment concerns"}

        parsed = fb.parse_reply(reply_text)
        if not parsed:
            return {
                "parsed": False,
                "reason": "reply did not match Confirm / Reject: <reason> / Classify: R<n>",
            }

        review_path = os.path.join(work, doc_id[:16], "review.json")
        if not os.path.exists(review_path):
            return {"parsed": False, "reason": "no review on file for that document"}
        with open(review_path, encoding="utf-8") as fh:
            review = json.load(fh)
        lexv = str(review.get("stats", {}).get("lexicon_version", "0"))

        by_id = {r["req_id"]: r for r in review.get("requirements", [])}
        r = by_id.get(req_id)
        if r is None:
            return {"parsed": False, "reason": f"no such provision in this review: {req_id}"}

        category = parsed.get("category") or r["category"]
        fb.record(store, fb.Decision(
            kind=parsed["kind"], category=category, reviewer=reviewer, doc_id=doc_id,
            req_id=r["req_id"], matched=r.get("matched") or [],
            quote=r.get("evidence_quote") or r.get("text", ""),
            reason=parsed.get("reason", ""), lexicon_version=lexv, source="comment-reply",
        ))
        return {"parsed": True, "recorded": True, "kind": parsed["kind"], "req_id": r["req_id"], "category": category}

    @app.post("/api/anchor-plan")
    async def anchor_plan(payload: Dict[str, Any]):
        """The anchor plan for one document (PLAN §4.1): where in the body
        text the add-in should search for each provision, and which hit is
        ours. Pure host-side computation over two strings — see cgate/anchor.py.
        The add-in still has to verify each hit before framing it (§4.2); this
        endpoint only plans, it never touches the document.
        """
        doc_id = (payload.get("doc_id") or "").strip()
        body_text = payload.get("body_text")
        if not doc_id:
            raise HTTPException(400, "doc_id is required")
        if not isinstance(body_text, str) or not body_text.strip():
            raise HTTPException(400, "body_text is required and must be non-empty")

        review_path = os.path.join(work, doc_id[:16], "review.json")
        if not os.path.exists(review_path):
            raise HTTPException(404, "no review on file for that document — run a review first")
        with open(review_path, encoding="utf-8") as fh:
            review = json.load(fh)

        provisions = [
            {"req_id": p["req_id"], "quote": p.get("quote", ""), "band": p.get("exposure", "routine")}
            for p in review.get("provisions", [])
        ]
        return anchor_.plan(provisions, body_text)

    @app.post("/api/submission")
    async def submission_review(
        files: List[UploadFile] = File(...),
        staff_register: Optional[UploadFile] = File(None),
        name: str = Form(""),
        parties: str = Form(""),
        solicitation_doc_id: str = Form(""),
    ):
        """Mark up the firm's own outgoing submission.

        The first file is the submission. Any further files are material
        received from or shared with a named third party, which is what makes
        shared-content detection possible.

        Every optional input switches a check on. Sending none of them produces
        a review that marks claims and nothing else, and the rubric it returns
        says so — see `cgate.submission`, which will not manufacture a
        commendation from a check that did not run.
        """
        from . import submission as sub

        if not files:
            raise HTTPException(400, "no submission supplied")

        tmp = tempfile.mkdtemp(prefix="cgate-sub-")
        try:
            paths: List[str] = []
            for i, up in enumerate(files):
                base = os.path.basename(up.filename or f"document-{i}")
                if i == 0 and name:
                    base = os.path.basename(name)
                ext = os.path.splitext(base)[1].lower()
                if ext not in ACCEPTED:
                    raise HTTPException(
                        415,
                        f"{base}: cgate reads {', '.join(sorted(ACCEPTED))}.",
                    )
                data = await up.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413, f"{base}: larger than {MAX_UPLOAD_BYTES // (1 << 20)} MB")
                dest = os.path.join(tmp, base)
                with open(dest, "wb") as fh:
                    fh.write(data)
                paths.append(dest)

            staff = None
            if staff_register is not None:
                raw = await staff_register.read()
                try:
                    staff = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HTTPException(400, f"staff register is not valid JSON: {exc}")
                if not isinstance(staff, list):
                    raise HTTPException(
                        400, "staff register must be a JSON list of {name, status, role}")

            declared = None
            if parties.strip():
                try:
                    declared = json.loads(parties)
                except json.JSONDecodeError:
                    declared = [x.strip() for x in parties.split(",") if x.strip()]

            # Coverage needs the solicitation's deliverables. They come from a
            # review already on file, so the submission is always measured
            # against a solicitation someone reviewed, never against a fresh
            # parse nobody saw.
            reqs = None
            if solicitation_doc_id.strip():
                sol_path = os.path.join(
                    work, solicitation_doc_id.strip()[:16], "review.json")
                if not os.path.exists(sol_path):
                    raise HTTPException(
                        404, "no review on file for that solicitation — review it first")
                reqs = _requirements_from_review(sol_path)

            review = sub.review_submission(
                paths[0],
                solicitation_requirements=reqs,
                solicitation_id=solicitation_doc_id.strip() or None,
                staff_register=staff,
                third_party_material=paths[1:],
                declared_parties=declared,
            )
            payload = review.to_dict()

            out_dir = os.path.join(work, review.submission_id[:16])
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "submission.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            return payload
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @app.post("/api/submission/anchor-plan")
    async def submission_anchor_plan(payload: Dict[str, Any]):
        """Where in the Word-side body text each mark should be placed.

        Reuses `cgate.anchor` unchanged: a mark and a provision are the same
        problem — a quote extracted from one representation of a document,
        searched for in another. The mark's kind stands in for the band, so the
        add-in gets its visual treatment from the same field either way.
        """
        doc_id = (payload.get("doc_id") or "").strip()
        body_text = payload.get("body_text")
        if not doc_id:
            raise HTTPException(400, "doc_id is required")
        if not isinstance(body_text, str) or not body_text.strip():
            raise HTTPException(400, "body_text is required and must be non-empty")

        path = os.path.join(work, doc_id[:16], "submission.json")
        if not os.path.exists(path):
            raise HTTPException(
                404, "no submission review on file for that document")
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)

        marks = [
            {"req_id": m["mark_id"], "quote": m.get("quote", ""), "band": m["kind"]}
            for m in stored.get("marks", [])
            if (m.get("quote") or "").strip()
        ]
        return anchor_.plan(marks, body_text)

    @app.get("/api/submission/{doc_id}")
    def submission_stored(doc_id: str):
        path = os.path.join(work, doc_id[:16], "submission.json")
        if not os.path.exists(path):
            raise HTTPException(404, "no submission review on file")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    @app.get("/api/draft/health")
    def draft_health():
        """Is a local model reachable, and which one.

        The pane asks before it offers to draft anything, so a missing model
        produces an absent button rather than a button that fails.
        """
        from . import draft as draft_

        ok, detail = draft_.available()
        return {"ok": ok, "detail": detail, "endpoint": draft_.endpoint()}

    @app.post("/api/draft")
    def draft_fix(payload: Dict[str, Any] = Body(...)):
        """Propose a replacement passage for one mark on OUR OWN submission.

        Two refusals are worth reading as design rather than validation:

        The `doc_id` must name a stored *submission* review. A drafting endpoint
        that accepted any passage would let the pane suggest edits to the
        client's tender, and editing the document the firm is bidding against is
        indefensible however the change is presented (PLAN §0.3).

        The mark is looked up in the stored review rather than taken from the
        request body. A caller that could post its own quote and finding could
        put arbitrary text through the model and get it back stamped with this
        tool's name.
        """
        from . import draft as draft_

        # The pane calls this id `submission_id`; the stored-review route calls the
        # same value `doc_id`. Accept both rather than make a caller guess.
        doc_id = str(payload.get("doc_id") or payload.get("submission_id") or "")
        mark_id = str(payload.get("mark_id") or "")
        if not doc_id or not mark_id:
            raise HTTPException(400, "doc_id and mark_id are both required")

        path = os.path.join(work, doc_id[:16], "submission.json")
        if not os.path.exists(path):
            raise HTTPException(
                404,
                "no submission review on file for that document. Fixes are only ever "
                "proposed for our own outgoing paper, never for a tender we received.",
            )
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)

        marks = {m.get("mark_id"): m for m in stored.get("marks", [])}
        mark = marks.get(mark_id)
        if mark is None:
            raise HTTPException(404, "no mark " + mark_id + " in that review")
        if mark.get("kind") == "commendation":
            raise HTTPException(
                400,
                "a commendation is a passage that passed a check against a named "
                "source. There is nothing to fix.",
            )

        try:
            result = draft_.draft_fix(mark)
        except draft_.DraftUnavailable as e:
            # 503 when trying again might work, 422 when it never will. The pane
            # renders the two differently, because "the model declined to invent
            # a source" is an answer and "the host is down" is not.
            raise HTTPException(
                503 if getattr(e, "retryable", True) else 422,
                str(e),
            ) from e

        return result.as_dict()

    @app.get("/manifest.xml")
    def manifest():
        path = os.path.join(ADDIN_DIR, "manifest.xml")
        if not os.path.exists(path):
            raise HTTPException(404, "manifest not built")
        return FileResponse(path, media_type="application/xml")

    @app.get("/", response_class=PlainTextResponse)
    def root():
        path = os.path.join(ADDIN_DIR, "taskpane.html")
        return FileResponse(path, media_type="text/html")

    if os.path.isdir(ADDIN_DIR):
        app.mount("/", StaticFiles(directory=ADDIN_DIR, html=True), name="addin")

    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    checklist: str = "config/checklist.example.yaml",
    store: str = ".cgate",
    work: str = os.path.join("out", "reviews"),
    insecure: bool = False,
) -> int:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"refusing to bind {host}. This server hands out the text of "
            "solicitations under review; it listens on loopback only.",
            file=sys.stderr,
        )
        return 2

    try:
        app = build_app(checklist, store, work)
    except ChecklistError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError:
        print(f"checklist not found: {checklist}", file=sys.stderr)
        return 2

    kwargs: Dict[str, Any] = {"host": host, "port": port, "log_level": "warning"}
    if insecure:
        scheme = "http"
        print("WARNING: serving plain HTTP. Word will refuse to load the task pane.")
    else:
        from . import certs

        cert_path, key_path = certs.ensure()
        kwargs["ssl_certfile"] = cert_path
        kwargs["ssl_keyfile"] = key_path
        scheme = "https"

    print(f"cgate review host on {scheme}://{host}:{port}")
    print(f"  checklist  {os.path.abspath(checklist)}")
    print(f"  reviews    {os.path.abspath(work)}")
    print(f"  ledger     {fb.ledger_path(store)}")
    print("  documents are read on this machine and never transmitted.")
    if not insecure:
        print("\nIf Word shows a certificate warning, run:  cgate trust")
    uvicorn.run(app, **kwargs)
    return 0
