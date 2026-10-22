"""
cgate.report — a ReviewResult as the page a Compliance Officer actually signs.

Three things this renderer refuses to do
----------------------------------------
It does not use the word "cleared", "passed", "compliant" or "satisfied" about
any item. Those are determinations, the CO makes them, and a document that
appears to make them for him is the exact failure this tool was designed
to prevent.

It does not let an unchecked item look like a clean one. "What was not checked"
is its own section, above the detail, with the reason for each. A reviewer
skimming the summary table must be unable to mistake silence for assurance.

It does not print a finding without its source. Every extracted provision shows
its verbatim quote, page, and clause number, because a finding a reviewer cannot
verify in thirty seconds is one they will either take on faith or ignore, and
both are worse than not reporting it.
"""

from __future__ import annotations

from .feedback import Calibration
from .model import ReviewResult, Status
from . import TOOL_NAME
from . import settings
from . import lexicon as lx

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

STATUS_LABEL = {
    Status.EVIDENCE_PRESENT: "Evidence present",
    Status.FLAG: "Flagged",
    Status.UNRESOLVED: "Not resolved",
    Status.NOT_APPLICABLE: "Not applicable",
    Status.REQUIRES_HUMAN: "For CO determination",
}

STATUS_MARK = {
    Status.EVIDENCE_PRESENT: "•",
    Status.FLAG: "!",
    Status.UNRESOLVED: "?",
    Status.NOT_APPLICABLE: "–",
    Status.REQUIRES_HUMAN: "→",
}

MODALITY_LABEL = {
    "obligation": "Obligation on us",
    "prohibition": "Prohibition",
    "permission": "Permission",
    "right_of_un": "Right reserved by the client",
    "condition": "Condition of eligibility",
    "unknown": "Unclassified",
}


def _quote(text: str, width: int = 400) -> str:
    t = " ".join(text.split())
    if len(t) > width:
        t = t[:width].rsplit(" ", 1)[0] + " […]"
    return t


def render(
    result: ReviewResult,
    calib: dict[str, Calibration] | None = None,
    lexicon_version: str = "0",
) -> str:
    L: list[str] = []
    a = L.append

    a("# Pre-Submission Compliance Review")
    a("")
    a("**This is not a compliance clearance.** It is evidence gathered for the")
    a("Compliance Officer, who determines clearance, conditions, or hold under")
    a(f"Document {settings.ref('procedure')}. Nothing below is a determination of the Compliance")
    a("Officer or of the Ethics and Compliance Committee.")
    a("")
    a("| | |")
    a("|---|---|")
    a(f"| Document | `{result.doc_path}` |")
    a(f"| Document ID (SHA-256) | `{result.doc_id}` |")
    a(f"| Checklist | {result.checklist_version} ({settings.ref('checklist')}) |")
    a(f"| Tool version | {result.tool_version} |")
    a(f"| Lexicon version | {lexicon_version} |")
    a(f"| Generated | {result.generated_at} |")
    a("")
    a("A marked-up copy of the document opened from the task pane")
    a("carries these same four values as custom document properties —")
    a("`CGATE_DocHash`, `CGATE_LexiconVersion`, `CGATE_ReviewedAt`, `CGATE_ToolVersion`.")
    a("To check whether a floating `.docx` is the document this review was signed")
    a("against, open its properties and compare `CGATE_DocHash` to the Document ID")
    a("above: a match means the markup was built from these exact bytes; a mismatch")
    a("or an absent property means it was not, regardless of what the file is named.")
    a("")

    # ---- summary ----------------------------------------------------------
    a("## 1. Checklist summary")
    a("")
    a("| # | Item | Handling | Status |")
    a("|---:|---|---|---|")
    for it in result.items:
        mark = STATUS_MARK[it.status]
        label = STATUS_LABEL[it.status]
        a(f"| {it.item_id} | {it.text} | {it.handling} | {mark} {label} |")
    a("")

    counts: dict[str, int] = {}
    for it in result.items:
        counts[it.status.value] = counts.get(it.status.value, 0) + 1
    parts = [f"{v} {STATUS_LABEL[Status(k)].lower()}" for k, v in sorted(counts.items())]
    a(f"**{len(result.items)} items: " + ", ".join(parts) + ".**")
    a("")

    # ---- what was not checked --------------------------------------------
    unchecked = [i for i in result.items if i.status is Status.UNRESOLVED]
    if unchecked:
        a("## 2. What this review did not establish")
        a("")
        a("These items were not resolved by this run. They are not findings of")
        a("compliance and they are not findings of non-compliance — they are gaps in")
        a("the evidence, and each needs closing before the submission goes out.")
        a("")
        for it in unchecked:
            a(f"**Item {it.item_id} — {it.text}**")
            for n in it.notes:
                a(f"  - {n}")
            a("")

    # ---- flags ------------------------------------------------------------
    flagged = [i for i in result.items if i.status in (Status.FLAG, Status.REQUIRES_HUMAN)]
    if flagged:
        a("## 3. Items requiring the Compliance Officer's determination")
        a("")
        for it in flagged:
            a(f"### Item {it.item_id} — {it.text}")
            a("")
            a(f"*{STATUS_LABEL[it.status]}. Handling: {it.handling}.*")
            if it.triggers:
                a("")
                a("Structural triggers fired: " + ", ".join(f"`{t}`" for t in it.triggers))
            a("")
            for n in it.notes:
                a(f"- {n}")
            a("")

    # ---- extracted provisions --------------------------------------------
    a("## 4. Provisions extracted from the solicitation")
    a("")
    if not result.requirements:
        a("No integrity, eligibility or conflict-of-interest provision was extracted.")
        a("")
    else:
        a(f"{len(result.requirements)} provision(s), each verified verbatim against the")
        a("source at the stated page. These are the **client's** rules. They define what")
        a("the firm is being asked to accept; they are not evidence about the firm.")
        a("")
        by_cat: dict[str, list] = {}
        for r in result.requirements:
            by_cat.setdefault(r.category, []).append(r)
        for cid in sorted(by_cat, key=lambda c: int(c[1:])):
            spec = lx.CATEGORIES.get(cid, {})
            name = spec.get("name", cid).replace("_", " ")
            items = spec.get("checklist_items", [])
            a(f"### {cid} — {name}")
            a("")
            a(f"*Bears on checklist item(s): {', '.join(str(i) for i in items)}.*")
            a("")
            for r in by_cat[cid]:
                where = f"p.{r.page}"
                if r.clause_number:
                    where += f", clause {r.clause_number}"
                a(f"**{r.req_id} — {MODALITY_LABEL.get(r.modality.value, r.modality.value)}"
                  f" · {r.actor.value} · {where}**")
                a("")
                a(f"> {_quote(r.evidence_quote or r.text)}")
                a("")
                for ex in r.exceptions:
                    state = "scope resolved" if ex.scope_resolved else "**scope UNRESOLVED**"
                    a(f"  - Carve-out (`{ex.marker}`, {state}): {_quote(ex.text, 220)}")
                    a(f"    - {ex.scope_note}")
                if r.incorporation:
                    inc = r.incorporation
                    a(f"  - Incorporates by reference, **not attached**: {inc['target']}"
                      + (f" — {inc['url']}" if inc.get("url") else ""))
                if r.triggers:
                    a("  - Triggers: " + ", ".join(f"`{t}`" for t in r.triggers))
                a("")

    # ---- calibration ------------------------------------------------------
    a("## 5. How much to trust each category")
    a("")
    if not calib:
        a("No reviewer decisions have been recorded yet, so no precision figure exists")
        a("for any category. Record decisions with `cgate feedback` as this review is")
        a("worked through; from the next review onward this section reports measured")
        a("precision per category, and the figures are the reviewer's own.")
        a("")
    else:
        a("Measured from reviewer decisions on previous reviews. Recall is an **upper")
        a("bound**: it counts only misses a reviewer reported, and a miss nobody")
        a("reported cannot be counted.")
        a("")
        a("| Category | Confirmed | Rejected | Reported misses | Precision | Recall (upper bound) |")
        a("|---|---:|---:|---:|---:|---:|")
        for cid in sorted(calib, key=lambda c: (len(c), c)):
            c = calib[cid]
            prec = f"{c.precision:.0%}" if c.precision is not None else "—"
            rec = f"{c.recall:.0%}" if c.recall is not None else "—"
            a(f"| {cid} | {c.confirmed} | {c.rejected} | {c.missed} | {prec} | {rec} |")
        a("")

    # ---- ingest notes -----------------------------------------------------
    if result.ingest_notes or result.errors:
        a("## 6. Source handling")
        a("")
        for n in result.ingest_notes:
            a(f"- {n}")
        for e in result.errors:
            flag = " **(blocking)**" if e.get("blocking") else ""
            a(f"- `{e.get('error_type')}`: {e.get('message')}{flag}")
        a("")

    # ---- the document against itself --------------------------------------
    if result.defects:
        a("## 7. Where the solicitation disagrees with itself")
        a("")
        a("These are findings about the **client's document**, not about the firm.")
        a("They bear on no checklist item and no status above. They are here because")
        a("a bid priced against the wrong one of two numbers is wrong before it is")
        a("written, and because the clarification deadline is the only moment at")
        a("which any of them can be fixed.")
        a("")
        by_check: dict[str, list] = {}
        for d in result.defects:
            by_check.setdefault(d.check, []).append(d)
        a("| Check | Raised |")
        a("|---|---:|")
        for k in sorted(by_check):
            a(f"| `{k}` | {len(by_check[k])} |")
        a("")
        for d in sorted(result.defects, key=lambda x: (SEVERITY_ORDER.get(x.severity, 9),
                                                       x.defect_id)):
            a(f"### {d.defect_id} — {d.title}")
            a("")
            a(f"*{d.severity} · `{d.check}`"
              + (f" · resolves to **{d.resolution}***" if d.resolution else "*"))
            a("")
            for line in d.detail.splitlines():
                a(line)
            a("")
            for q in d.quotes[:6]:
                a(f"> {_quote(q, 320)}")
                a("")
        a("")

    # ---- coverage ---------------------------------------------------------
    if result.coverage:
        c = result.coverage
        total = c.get("blocks_total", 0)
        done = c.get("blocks_compiled", 0)
        a("## 8. What the compiler read, and what it did not")
        a("")
        a("A checker built on a list of subjects cannot tell you what it missed,")
        a("because everything off the list is invisible to it. This one can, so it")
        a("does. The count below is of source blocks, and the blocks that produced")
        a("neither a commitment nor a quantity are listed in `review.json` under")
        a("`coverage.uncompiled` — by id, section and first 240 characters — for a")
        a("reviewer to scan.")
        a("")
        a("| | |")
        a("|---|---:|")
        a(f"| Blocks in the source | {total} |")
        a(f"| Blocks that entered a check | {done} |")
        a(f"| Blocks that entered no check | {c.get('blocks_uncompiled', 0)} |")
        a(f"| Commitments compiled | {c.get('commitments', 0)} |")
        a(f"| Quantities compiled | {c.get('measurands', 0)} |")
        a("")
        if total:
            a(f"**{done} of {total} blocks ({done / total:.0%}) entered at least one")
            a("check.** The remainder are headings, addresses, form fields and prose")
            a("that carries no modal verb and no number. That they were not checked")
            a("is not a finding that they are sound.")
            a("")
        for n in c.get("notes", []):
            a(f"- {n}")
        a("")

    a("---")
    a("")
    a(f"Prepared by {TOOL_NAME}, under its review-process notes. The system")
    a("gathers evidence; a named human decides. Determination, signature and date:")
    a("")
    a("Compliance Officer: ______________________  Date: ____________")
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# The assembled gate file
# ---------------------------------------------------------------------------


def render_gate(gf, calibration: Calibration | None = None,
                lexicon_version: str | int = 0) -> str:
    """A `gate.GateFile` as the page the Compliance Officer signs.

    Ordering is the argument. What stands in the way comes first, because it is
    the only part that is actionable before the deadline. The evidence gathered
    comes after it, and the extracted provisions come last — they are the
    longest section and the least urgent.
    """
    import os as _os

    from .gate import blocking_reasons

    p = gf.pursuit
    out: list[str] = []
    w = out.append

    # Sections are numbered as they are emitted. Some are conditional, and a
    # file that jumps from 5 to 7 reads as a page somebody removed.
    _n = [0]

    def section(title: str) -> None:
        _n[0] += 1
        w(f"## {_n[0]}. {title}")

    w("# Pre-Submission Compliance File")
    w("")
    w("**This is not a compliance clearance.** It is evidence gathered for the")
    w("Compliance Officer, who determines clearance, conditions, or hold under")
    w(f"Document {settings.ref('procedure')}. Nothing below is a determination of the")
    w("Compliance Officer or of the Ethics and Compliance Committee.")
    w("")
    w("| | |")
    w("|---|---|")
    w(f"| Pursuit | `{gf.pursuit_id}` |")
    w(f"| Submission | {p.get('title', '')} |")
    w(f"| Tender reference | `{p.get('tender_reference') or 'not recorded'}` |")
    w(f"| Jurisdiction | {p.get('jurisdiction') or 'not recorded'} |")
    w(f"| Notified | {p.get('notified_at', '')} by {p.get('notified_by', '')} |")
    w(f"| Submission deadline | {p.get('submission_deadline', '')} |")
    w(f"| Review track | {p.get('review_track', '')} |")
    w(f"| State | {gf.clocks.get('state', '')} |")
    w(f"| Solicitation | `{gf.review.doc_path}` |")
    w(f"| Document ID (SHA-256) | `{gf.review.doc_id}` |")
    w(f"| Checklist | {gf.review.checklist_version} ({settings.ref('checklist')}) |")
    w(f"| Tool version | {gf.review.tool_version} |")
    w(f"| Generated | {gf.review.generated_at} |")
    w("")

    # --- 1. what stands in the way -----------------------------------------
    reasons = blocking_reasons(gf)
    section("What stands in the way")
    w("")
    if not reasons:
        w("Nothing recorded in this file blocks progression. That is a statement")
        w("about the checks that ran, not a clearance.")
    else:
        w("Each line is something to resolve, or to proceed despite as a recorded")
        w("decision. The system does not decide which.")
        w("")
        for r in reasons:
            w(f"- {r}")
    w("")

    # --- 2. clocks ----------------------------------------------------------
    c = gf.clocks
    section(f"Clocks ({settings.ref('procedure')} §4.3, §5.3)")
    w("")
    w("| | |")
    w("|---|---|")
    w(f"| Notification lead | {c['notification_lead_days']} business day(s), "
      f"{c['notification_required_days']} required |")
    w(f"| Review period | {c['review_days_elapsed']} of {c['review_days_allowed']} "
      f"day(s) used, due {c['review_due']} |")
    if c.get("paused_days"):
        w(f"| Paused | {c['paused_days']} business day(s) on information requests |")
    w("")
    if not c.get("holiday_calendar_supplied"):
        w("No holiday calendar was supplied. Elapsed business days may be")
        w("under-counted and are never over-counted, so a breach shown here is")
        w("real and a breach not shown may still exist.")
        w("")
    for b in c.get("breaches", []):
        w(f"- **Breach — {b['clock']}.** {b['message']}")
    if c.get("breaches"):
        w("")

    # --- 3. checklist -------------------------------------------------------
    section("Checklist summary")
    w("")
    w("| # | Item | Handling | Status |")
    w("|---:|---|---|---|")
    for item in gf.review.items:
        w(f"| {item.item_id} | {item.text} | {item.handling} | "
          f"{STATUS_MARK[item.status]} {STATUS_LABEL[item.status]} |")
    w("")
    counts: dict[str, int] = {}
    for item in gf.review.items:
        counts[item.status.value] = counts.get(item.status.value, 0) + 1
    parts = [f"{n} {STATUS_LABEL[Status(k)].lower()}"
             for k, n in sorted(counts.items()) if n]
    w(f"**{len(gf.review.items)} items: {', '.join(parts)}.**")
    w("")

    # --- 4. named parties ---------------------------------------------------
    section("Named third parties")
    w("")
    if not gf.parties:
        w("No subcontractor, consortium partner or agent is named on this pursuit.")
        w("If that is wrong, the gate has been given an incomplete picture and")
        w("items 6 to 9 cannot be relied on.")
    else:
        w("| Party | Role | Declaration | Live collisions |")
        w("|---|---|---|---|")
        for party in gf.parties:
            state = party.declaration_state if party.declarable else "n/a"
            w(f"| {party.legal_name} | {party.role} | {state} | "
              f"{len(party.collisions)} |")
        w("")
        for party in gf.parties:
            for col in party.collisions:
                w(f"- **{party.legal_name}** is also named on "
                  f"`{col['other_pursuit_id']}` ({col['other_pursuit_title']}, "
                  f"client {col['other_client']}) as {col['role_there']} — "
                  f"{col['severity'].replace('_', ' ')}.")
    w("")

    # --- 5. screening -------------------------------------------------------
    section("Screening")
    w("")
    for s in gf.screenings:
        if not s.ok:
            err = s.errors[0]
            retry = "retryable" if err.get("retryable") else "not retryable"
            w(f"- **{s.subject} — NOT SCREENED.** {err.get('error_type')}: "
              f"{err.get('message')} ({retry}). "
              "A screening that did not run is not a clean screening.")
            continue
        if not s.hits:
            w(f"- {s.subject}: no match in `{s.dataset}` at {s.queried_at} "
              f"(source `{s.source}`).")
            continue
        w(f"- **{s.subject}: {len(s.hits)} hit(s)** in `{s.dataset}` "
          f"at {s.queried_at}.")
        for hit in s.hits[:5]:
            named = ", ".join(hit["datasets"]) or "no dataset named"
            w(f"  - {hit['score']:.2f} — {hit['caption']} [{named}]")
    w("")

    # --- 6. shared content --------------------------------------------------
    if gf.conflict_scans:
        section("Shared-content detection")
        w("")
        w("Compares the outgoing submission against material received from or")
        w("shared with a named third party. This is the mechanism that addresses")
        w("undisclosed content-sharing directly.")
        w("")
        for s in gf.conflict_scans:
            plural = "y" if len(s.metadata_matches) == 1 else "ies"
            w(f"**{_os.path.basename(s.doc_a)} vs {_os.path.basename(s.doc_b)}** — "
              f"estimated text overlap {s.text_similarity:.2%}, "
              f"{len(s.exact_matches)} exact passage match(es), "
              f"{len(s.image_matches)} image match(es), "
              f"{len(s.metadata_matches)} shared document propert{plural}.")
            for m in s.metadata_matches:
                w(f"  - shared `{m['field']}`: {m['value']} ({m['weight']})")
            for m in s.exact_matches[:3]:
                w(f"  - {m['words']} identical words: {_quote(m['text'], 200)}")
            for m in s.image_matches[:3]:
                identical = ", byte-identical" if m["identical_bytes"] else ""
                w(f"  - image distance {m['hamming_distance']}/64{identical}")
            w("")

    # --- 7. attendance ------------------------------------------------------
    if gf.attendance:
        section("Pre-bid engagement")
        w("")
        w("| Date | Kind | Attendee | Resolved to registry |")
        w("|---|---|---|---|")
        for a in gf.attendance:
            resolved = "yes" if a["attendee_entity"] else "no"
            w(f"| {a['occurred_at']} | {a['kind']} | {a['attendee_name']} | "
              f"{resolved} |")
        w("")

    # --- 8. notes -----------------------------------------------------------
    if gf.notes:
        section("Limits of this file")
        w("")
        for n in gf.notes:
            w(f"- {n}")
        w("")

    # --- 9. the review itself ----------------------------------------------
    w("---")
    w("")
    section("Item 12 — the client's procurement integrity rules")
    w("")
    w("The full extraction, with every provision quoted verbatim at its cited")
    w("offsets, follows below.")
    w("")
    w(render(gf.review, calibration, lexicon_version))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The examiner's marks on our own submission
# ---------------------------------------------------------------------------


def render_marks(review) -> str:
    """A `submission.SubmissionReview` as a grade sheet.

    Dense on purpose. The faults come first, the credit last, and the checks
    that did not run are their own section rather than a footnote — a reader
    who stops after the first screen must have met the bad news and must not be
    able to mistake an unexamined dimension for a sound one.
    """
    from .submission import (COMMENDATION, CORRECTION, MARK_GLYPH, QUERY)

    r = review.rubric
    counts = review.counts()
    out: list[str] = []
    w = out.append

    w("# Submission Review — examiner's marks")
    w("")
    w("**Not a compliance clearance and not a determination.** These are marks on")
    w("the firm's own document, for the author and the Compliance Officer to act on.")
    w("")
    w("| | |")
    w("|---|---|")
    w(f"| Submission | `{r.submission}` |")
    w(f"| Document ID (SHA-256) | `{review.submission_id}` |")
    if r.solicitation_id:
        w(f"| Against solicitation | `{r.solicitation_id}` |")
    w(f"| Corrections | **{counts[CORRECTION]}** |")
    w(f"| Queries | {counts[QUERY]} |")
    w(f"| Verified | {counts[COMMENDATION]} |")
    w("")

    if r.checks_skipped:
        w(f"## Checks that did not run ({len(r.checks_skipped)} of 5)")
        w("")
        w("A mark that was never made is not a passage that was found sound.")
        w("")
        for sk in r.checks_skipped:
            w(f"- **{sk.rule}** — needs {sk.needs}. {sk.consequence}")
        w("")

    for kind, title in ((CORRECTION, "Corrections"), (QUERY, "Queries"),
                        (COMMENDATION, "Verified")):
        marks = review.by_kind(kind)
        if not marks:
            continue
        w(f"## {MARK_GLYPH[kind]} {title} ({len(marks)})")
        w("")
        for m in marks:
            where = m.locator or "document-wide"
            w(f"**{m.heading}** · {where}")
            w("")
            w(f"{m.detail}")
            if m.quote:
                w("")
                w("> " + _quote(m.quote, 300).replace("\n", " "))
            w("")
            w(f"*How this was reached:* {m.evidence}")
            if m.checklist_items:
                w(f"*Bears on checklist item(s):* "
                  f"{', '.join(str(i) for i in m.checklist_items)}.")
            w("")

    if r.mandatory_unaddressed:
        w(f"## Required but not found in the submission ({len(r.mandatory_unaddressed)})")
        w("")
        w("An omission has no passage to mark, so it is listed here rather than in")
        w("the margin. Inventing an anchor for it would be a fabrication.")
        w("")
        w("| Clause | Page | Requirement | Why |")
        w("|---|---:|---|---|")
        for e in r.mandatory_unaddressed:
            text = (e["text"][:120] + "…") if len(e["text"]) > 120 else e["text"]
            w(f"| {e.get('clause') or '—'} | {e.get('page') or '—'} | "
              f"{text.replace('|', '/')} | {e['why']} |")
        w("")

    if r.mandatory_total:
        w(f"**Coverage: {len(r.mandatory_addressed)} of {r.mandatory_total} "
          f"deliverable requirement(s) addressed.** Addressed means the submission")
        w("uses the requirement's own distinctive terms, not that the answer is")
        w("sufficient. Sufficiency is a reader's judgment.")
        w("")

    if review.notes:
        w("## Limits")
        w("")
        for n in review.notes:
            w(f"- {n}")
        w("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The compiled specification
# ---------------------------------------------------------------------------


def render_spec(spec, doc_name: str = "") -> str:
    """A `SpecResult` as the page a proposal manager works from.

    Ordering follows what has to happen first. The defects come at the top
    because several of them can only be fixed by asking the client before the
    clarification deadline. The requirement ledger follows, because that is what
    the compliance matrix is built from. Coverage is last and is not optional —
    it is the section that lets a reader judge everything above it.
    """
    L: list[str] = []
    a = L.append

    a("# Compiled Solicitation")
    a("")
    a("**This is not a compliance clearance and not a bid/no-bid recommendation.**")
    a("It is the client's document read as a specification: what it requires, what")
    a("it measures, and where it contradicts itself. Every line below quotes the")
    a("source at the offsets it was found at.")
    a("")
    a("| | |")
    a("|---|---|")
    a(f"| Document | `{doc_name or spec.doc_path}` |")
    a(f"| Document ID (SHA-256) | `{spec.doc_id}` |")
    a(f"| Commitments compiled | {len(spec.commitments)} |")
    a(f"| Quantities compiled | {len(spec.measurands)} |")
    a(f"| Defects raised | {len(spec.defects)} |")
    a("")

    a("## 1. Where the document disagrees with itself")
    a("")
    if not spec.defects:
        a("No check raised a defect. That is not a finding that the document is")
        a("consistent — it is a finding that these eight checks, over the blocks")
        a("listed in §3 as compiled, found nothing. The checks that ran are named")
        a("at the end of this section.")
        a("")
    else:
        by_check: dict[str, int] = {}
        for d in spec.defects:
            by_check[d.check] = by_check.get(d.check, 0) + 1
        a("| Check | Raised |")
        a("|---|---:|")
        for k in sorted(by_check):
            a(f"| `{k}` | {by_check[k]} |")
        a("")
        for d in sorted(spec.defects,
                        key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.defect_id)):
            a(f"### {d.defect_id} — {d.title}")
            a("")
            a(f"*{d.severity} · `{d.check}`"
              + (f" · resolves to **{d.resolution}***" if d.resolution else "*"))
            a("")
            for line in d.detail.splitlines():
                a(line)
            a("")
            for q in d.quotes[:6]:
                a(f"> {_quote(q, 320)}")
                a("")

    a("## 2. What the document requires")
    a("")
    a("Every sentence carrying a modal verb, grouped by the section it sits in.")
    a("A commitment marked *(from list stem)* took its modal from the sentence")
    a("introducing the list it belongs to — \"the Consultant shall:\" — rather than")
    a("from its own words.")
    a("")
    by_section: dict[str, list] = {}
    for c in spec.commitments:
        by_section.setdefault(c.section, []).append(c)
    for section, group in by_section.items():
        kind = group[0].section_kind
        a(f"### {section} · `{kind}`")
        a("")
        for c in group:
            bits = [c.modality.value, c.actor.value]
            if c.disqualifying:
                bits.append("**bid-ending**")
            if c.modal_inherited:
                bits.append("*(from list stem)*")
            a(f"- {' · '.join(bits)} — {_quote(c.text, 300)}")
        a("")

    a("## 3. What the compiler read, and what it did not")
    a("")
    total, done = spec.blocks_total, spec.blocks_compiled
    a("| | |")
    a("|---|---:|")
    a(f"| Blocks in the source | {total} |")
    a(f"| Blocks that entered a check | {done} |")
    a(f"| Blocks that entered no check | {len(spec.uncompiled)} |")
    a("")
    if total:
        a(f"**{done} of {total} blocks ({done / total:.0%}) entered at least one")
        a("check.** A checker built on a list of subjects cannot report this number")
        a("at all, because what is off the list never reaches a count. The blocks")
        a("that entered no check are listed below in full; that they were not")
        a("checked is not a finding that they are sound.")
        a("")
    for u in spec.uncompiled:
        a(f"- `{u['block_id']}` · {u['section']} — {_quote(u['text'], 200)}")
    a("")
    for n in spec.notes:
        a(f"- {n}")
    a("")
    a("---")
    a("")
    a(f"Compiled by {TOOL_NAME}, under its review-process notes. Nothing")
    a("here is a determination of the Compliance Officer or of the Ethics and")
    a("Compliance Committee.")
    a("")
    return "\n".join(L)
