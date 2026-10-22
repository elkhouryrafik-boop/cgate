"""
cgate.cli — the four things a person does with this tool.

    cgate review CONTRACT.pdf              a solicitation in, a review out
    cgate feedback --review out/review.json  record what the reviewer decided
    cgate learn                             see what those decisions support changing
    cgate bench                             measure the extractor against fixtures
    cgate serve                             the local server the Word add-in talks to
    cgate trust                             trust the localhost certificate Word requires
    cgate addin --install                   register the task pane with Word

`review` is the whole product. The rest exist so that the first one gets better,
stays honest about how good it is, and can be read in the margin of the document
it concerns rather than in a terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import feedback as fb
from .adjudicate import ChecklistError, load_checklist, summarise
from .datarequest import render as render_request
from .model import Status
from .pipeline import run_review
from .report import render

DEFAULT_CHECKLIST = "config/checklist.example.yaml"
DEFAULT_STORE = ".cgate"


# ---------------------------------------------------------------------------


def cmd_review(args: argparse.Namespace) -> int:
    try:
        result, lexicon_version, blocking = run_review(
            args.contract, args.checklist, args.bundle, args.store
        )
    except FileNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 2
    except ChecklistError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 3

    items = result.items
    kept = result.requirements
    n_clauses = result.stats["clauses"]
    n_dropped = result.stats["requirements_dropped"]

    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, "review.json")
    md_path = os.path.join(args.out, "review.md")

    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(result.to_json())

    calib = fb.calibration(fb.load_ledger(args.store))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render(result, calib or None, lexicon_version))

    # The gap is only useful if somebody knows what would close it.
    req_path = os.path.join(args.out, "evidence-request.md")
    with open(req_path, "w", encoding="utf-8") as fh:
        fh.write(render_request(result, load_checklist(args.checklist),
                                requested_by=args.requested_by, due=args.due))

    counts = summarise(items)
    names = ", ".join(os.path.basename(p) for p in args.contract)
    print(f"{names}: {result.stats['pages']}pp, {n_clauses} clauses, "
          f"{len(kept)} provisions ({n_dropped} dropped by the gate)")
    print(f"  {counts[Status.EVIDENCE_PRESENT.value]} evidence present, "
          f"{counts[Status.FLAG.value]} flagged, "
          f"{counts[Status.REQUIRES_HUMAN.value]} for CO determination, "
          f"{counts[Status.UNRESOLVED.value]} unresolved")
    if blocking:
        print("  BLOCKING: " + "; ".join(f"{e['error_type']}: {e['message']}" for e in blocking))
    print(f"  {md_path}")
    print(f"  {json_path}")
    print(f"  {req_path}")

    # A run that resolved nothing is not a successful run.
    return 1 if blocking else 0


# ---------------------------------------------------------------------------


def cmd_spec(args: argparse.Namespace) -> int:
    """Compile a solicitation and lint it, without adjudicating anything.

    A proposal manager needs the requirement ledger and the contradictions on
    the day the tender drops, which is weeks before there is a pursuit to open
    or evidence to gather. `review` still runs this and files the result; this
    command is the same compilation with no checklist attached to it.
    """
    from .ingest import ingest
    from .lint import lint
    from .report import render_spec
    from .spec import compile_spec

    try:
        doc = ingest(args.document)
    except FileNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 2

    spec = lint(doc, compile_spec(doc))

    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, "spec.json")
    md_path = os.path.join(args.out, "spec.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(spec.to_json())
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_spec(spec, os.path.basename(args.document)))

    high = sum(1 for d in spec.defects if d.severity == "high")
    pct = (spec.blocks_compiled / spec.blocks_total) if spec.blocks_total else 0
    print(f"{os.path.basename(args.document)}: {spec.blocks_total} blocks, "
          f"{len(spec.commitments)} commitments, {len(spec.measurands)} quantities")
    print(f"  {len(spec.defects)} defect(s), {high} high severity")
    print(f"  coverage: {spec.blocks_compiled}/{spec.blocks_total} blocks "
          f"({pct:.0%}) entered a check")
    for d in sorted(spec.defects, key=lambda x: (x.severity != "high", x.defect_id)):
        print(f"    [{d.severity}] {d.check}: {d.title}")
    print(f"  {md_path}")
    print(f"  {json_path}")
    return 0


def cmd_corpus(args: argparse.Namespace) -> int:
    """Fetch, list and re-check the reference corpus.

    The corpus is what lets a review say which instrument a tender means when
    it incorporates one by reference without attaching it. Fetching it is not
    the interesting part; `verify --remote` is, because it is the only thing
    that notices when a publisher moves an instrument the submission still cites.
    """
    from . import corpus as cp

    if args.corpus_action == "fetch":
        def show(e):
            mark = "ok  " if e.ok() else "FAIL"
            size = f"{e.bytes/1024:,.0f} kB" if e.bytes else "-"
            print(f"  [{mark}] {e.id:44s} {size:>10s}  {e.error or e.content_type}")

        print(f"fetching into {os.path.join(cp.corpus_root(), 'files')}")
        entries = cp.fetch_all(only=args.only or None, progress=show)
        if args.only:
            # Keep the entries this run did not touch.
            kept = [e for e in cp.load_manifest() if e.id not in set(args.only)]
            entries = kept + entries
        path = cp.save_manifest(entries)
        ok = [e for e in entries if e.ok()]
        bad = [e for e in entries if not e.ok()]
        print(f"\n{len(ok)} fetched, {len(bad)} failed")
        for e in bad:
            print(f"  FAILED {e.id}: {e.error}")
        _, unavailable = cp.load_sources()
        if unavailable:
            print(f"\n{len(unavailable)} instrument(s) named but not fetchable "
                  "— licensed, purchase required:")
            for u in unavailable:
                print(f"  - {u['id']}: {u['title']}")
        print(f"\n  {path}")
        return 0 if not bad else 1

    if args.corpus_action == "list":
        entries = cp.load_manifest()
        if not entries:
            print("corpus is empty — run `cgate corpus fetch`")
            return 1
        by_cat: dict[str, list] = {}
        for e in entries:
            by_cat.setdefault(e.category, []).append(e)
        for cat in sorted(by_cat):
            print(f"\n{cat}")
            for e in sorted(by_cat[cat], key=lambda x: x.id):
                state = e.sha256[:12] if e.ok() else f"FAILED {e.error}"
                print(f"  {e.id:44s} {e.issuer:<12s} {state}")
                print(f"    {e.title}")
                print(f"    retrieved {e.retrieved_at or '-'} · {e.bytes:,} bytes "
                      f"· stated edition: {e.edition_hint or '-'}")
        _, unavailable = cp.load_sources()
        if unavailable:
            print("\nnot fetchable — licensed, purchase required")
            for u in unavailable:
                print(f"  {u['id']:44s} {u['title']}")
        return 0

    if args.corpus_action == "verify":
        def show(e):
            if e.error:
                print(f"  unreachable {e.id}: {e.error}")

        res = cp.verify(remote=args.remote, progress=show if args.remote else None)
        print(f"{res.checked} entries checked, {res.unchanged} unchanged")
        for i in res.missing_local:
            print(f"  MISSING locally: {i} — re-fetch")
        for i in res.corrupt_local:
            print(f"  HASH MISMATCH on disk: {i} — the cached file is not what "
                  "the manifest recorded")
        for c in res.changed_upstream:
            label = ("RE-ISSUED (same stated edition, different bytes)"
                     if c.get("kind") == "reissued_bytes" else
                     "EDITION CHANGED UPSTREAM")
            print(f"  {label}: {c['id']} — {c['title']}")
            print(f"    held since {c['was_retrieved']}  {c['was'][:12]}")
            print(f"    now served              {c['now'][:12]}")
            if c["declared_now"]:
                print(f"    first page now reads: {c['declared_now'][:120]}")
        for u in res.unreachable:
            print(f"  UNREACHABLE: {u['id']} — {u['error']}")
        problems = (res.missing_local or res.corrupt_local
                    or res.changed_upstream or res.unreachable)
        if not problems:
            print("every held instrument still matches what its publisher serves"
                  if args.remote else
                  "every held file still hashes to what the manifest recorded "
                  "(local only: run with --remote to ask the publishers)")
        return 1 if problems else 0

    return 2


def cmd_feedback(args: argparse.Namespace) -> int:
    with open(args.review, encoding="utf-8") as fh:
        review = json.load(fh)
    by_id = {r["req_id"]: r for r in review.get("requirements", [])}
    store = args.store
    lexv = str(review.get("stats", {}).get("lexicon_version", "0"))
    n = 0

    for kind, ids in (("confirm", args.confirm or []), ("reject", args.reject or [])):
        for rid in ids:
            r = by_id.get(rid)
            if r is None:
                print(f"no such requirement in this review: {rid}", file=sys.stderr)
                return 2
            fb.record(
                store,
                fb.Decision(
                    kind=kind,
                    category=r["category"],
                    reviewer=args.reviewer,
                    doc_id=review["doc_id"],
                    req_id=rid,
                    matched=r.get("matched") or [],
                    quote=r.get("evidence_quote") or r.get("text", ""),
                    reason=args.reason,
                    lexicon_version=lexv,
                ),
            )
            n += 1

    if args.miss:
        if not args.category:
            print("--miss requires --category (e.g. R5)", file=sys.stderr)
            return 2
        fb.record(
            store,
            fb.Decision(
                kind="miss",
                category=args.category,
                reviewer=args.reviewer,
                doc_id=review["doc_id"],
                quote=args.miss,
                reason=args.reason,
                lexicon_version=lexv,
            ),
        )
        n += 1

    print(f"{n} decision(s) recorded in {fb.ledger_path(store)}")
    return 0


# ---------------------------------------------------------------------------


def cmd_learn(args: argparse.Namespace) -> int:
    ledger = fb.load_ledger(args.store)
    if not ledger:
        print(f"no decisions recorded yet in {fb.ledger_path(args.store)}")
        return 0

    calib = fb.calibration(ledger)
    print(f"{len(ledger)} decision(s) across {len(calib)} categor(ies)\n")
    print(f"{'cat':<5} {'confirmed':>9} {'rejected':>9} {'misses':>7} {'precision':>10}")
    for cid in sorted(calib, key=lambda c: (len(c), c)):
        c = calib[cid]
        prec = f"{c.precision:.0%}" if c.precision is not None else "—"
        print(f"{cid:<5} {c.confirmed:>9} {c.rejected:>9} {c.missed:>7} {prec:>10}")

    props = fb.proposals(ledger)
    if not props:
        print("\nNo change to the lexicon is supported by these decisions yet.")
        return 0

    print(f"\n{len(props)} proposal(s). None is applied until approved.\n")
    for i, p in enumerate(props):
        print(f"[{i}] {p.kind}  {p.category}  support={p.support}")
        print(f"    pattern:  {p.pattern}")
        print(f"    because:  {p.rationale}")
        print(f"    evidence: {p.evidence[:160]}")
        print()

    if not args.approve:
        print("To apply: cgate learn --approve 0,2 --approver \"<name>\"")
        return 0

    if not args.approver:
        print("--approve requires --approver: a named human owns this change.", file=sys.stderr)
        return 2

    try:
        chosen = [props[int(i)] for i in args.approve.split(",") if i.strip() != ""]
    except (ValueError, IndexError):
        print(f"--approve must be indices from the list above (0-{len(props) - 1})", file=sys.stderr)
        return 2

    version = fb.approve(args.store, chosen, args.approver)
    print(f"{len(chosen)} proposal(s) approved by {args.approver}.")
    print(f"lexicon version is now {version}; it is recorded on every future review.")
    return 0


# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import serve

    return serve(
        host=args.host,
        port=args.port,
        checklist=args.checklist,
        store=args.store,
        work=args.work,
        insecure=args.insecure,
    )


# ---------------------------------------------------------------------------


def cmd_trust(args: argparse.Namespace) -> int:
    from . import certs

    if args.remove:
        return certs.untrust()
    cert_path, _ = certs.ensure(force=args.regenerate)
    return certs.trust(cert_path)


# ---------------------------------------------------------------------------


def cmd_addin(args: argparse.Namespace) -> int:
    from . import sideload

    if args.remove:
        return sideload.remove()
    if args.install:
        return sideload.install()
    return sideload.status()


# ---------------------------------------------------------------------------


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import run_anchor_bench, run_bench

    rc = run_bench(args.fixtures, args.store, verbose=not args.quiet)

    if args.anchor_doc or args.anchor_body:
        if not (args.anchor_doc and args.anchor_body):
            print("--anchor-doc and --anchor-body must be given together", file=sys.stderr)
            return 2
        print()
        run_anchor_bench(args.anchor_doc, args.anchor_body, args.checklist, args.store)

    return rc


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cgate",
        description="Pre-submission compliance review support. Gathers evidence; "
                    "a named human decides.",
    )
    p.add_argument("--store", default=DEFAULT_STORE,
                   help="feedback ledger and approved lexicon overrides (default: .cgate)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("review", help="review a solicitation against the checklist")
    r.add_argument("contract", nargs="+",
                   help="the solicitation, then any annexes it incorporates "
                        "(PDF, DOCX, TXT or MD)")
    r.add_argument("--checklist", default=DEFAULT_CHECKLIST)
    r.add_argument("--bundle", default=None,
                   help="JSON of externally-gathered evidence, keyed by item id")
    r.add_argument("--out", default="out")
    r.add_argument("--requested-by", default="",
                   help="name on the evidence request generated alongside the review")
    r.add_argument("--due", default="",
                   help="date the evidence request asks for a response by")
    r.set_defaults(func=cmd_review)

    sp = sub.add_parser("spec", help="compile a solicitation and check it against itself")
    sp.add_argument("document", help="the solicitation (PDF, DOCX, TXT or MD)")
    sp.add_argument("--out", default="out")
    sp.set_defaults(func=cmd_spec)

    cs = sub.add_parser("corpus", help="the reference instruments, fetched and hashed")
    cs_sub = cs.add_subparsers(dest="corpus_action", required=True)
    cs_fetch = cs_sub.add_parser("fetch", help="download every source in sources.yaml (or sources.example.yaml)")
    cs_fetch.add_argument("--only", nargs="*", default=[],
                          help="fetch just these source ids")
    cs_sub.add_parser("list", help="what is held, with hashes and retrieval dates")
    cs_verify = cs_sub.add_parser(
        "verify", help="re-hash what is held; --remote also asks the publisher")
    cs_verify.add_argument("--remote", action="store_true",
                           help="re-fetch each source and report any that moved")
    cs.set_defaults(func=cmd_corpus)

    f = sub.add_parser("feedback", help="record reviewer decisions on a review")
    f.add_argument("--review", required=True, help="path to review.json")
    f.add_argument("--confirm", nargs="*", metavar="REQ_ID")
    f.add_argument("--reject", nargs="*", metavar="REQ_ID")
    f.add_argument("--miss", default=None,
                   help="verbatim quote of a provision the rules failed to find")
    f.add_argument("--category", default=None, help="category for --miss, e.g. R5")
    f.add_argument("--reason", default="")
    f.add_argument("--reviewer", default=os.environ.get("USER") or os.environ.get("USERNAME") or "")
    f.set_defaults(func=cmd_feedback)

    ln = sub.add_parser("learn", help="calibration and lexicon proposals from the ledger")
    ln.add_argument("--approve", default=None, metavar="I,J",
                   help="indices of proposals to apply")
    ln.add_argument("--approver", default=None, help="the person who owns this change")
    ln.set_defaults(func=cmd_learn)

    s_ = sub.add_parser("serve", help="run the local review server the Word add-in talks to")
    s_.add_argument("--host", default="127.0.0.1",
                    help="loopback only by default; documents never leave this machine")
    s_.add_argument("--port", type=int, default=8765)
    s_.add_argument("--checklist", default=DEFAULT_CHECKLIST)
    s_.add_argument("--work", default=os.path.join("out", "reviews"),
                    help="where review artifacts for each document are written")
    s_.add_argument("--insecure", action="store_true",
                    help="serve plain HTTP. Word will refuse to load the pane; "
                         "for browser testing only")
    s_.set_defaults(func=cmd_serve)

    t = sub.add_parser("trust", help="trust the local certificate so Word loads the pane")
    t.add_argument("--remove", action="store_true", help="remove it again")
    t.add_argument("--regenerate", action="store_true", help="issue a fresh certificate first")
    t.set_defaults(func=cmd_trust)

    ad = sub.add_parser("addin", help="register the Word add-in with this machine's Word")
    ad.add_argument("--install", action="store_true",
                    help="point Word at the manifest (per-user registry value)")
    ad.add_argument("--remove", action="store_true", help="unregister it again")
    ad.set_defaults(func=cmd_addin)

    b = sub.add_parser("bench", help="measure the extractor against labelled fixtures")
    b.add_argument("--fixtures", default=os.path.join("bench", "fixtures"))
    b.add_argument("--quiet", action="store_true")
    b.add_argument("--checklist", default=DEFAULT_CHECKLIST)
    b.add_argument("--anchor-doc", default=None,
                   help="a solicitation to also run the §4.1 anchor planner against")
    b.add_argument("--anchor-body", default=None,
                   help="saved Word-side body text for --anchor-doc (see cgate/bench.py "
                        "run_anchor_bench for how to capture one)")
    b.set_defaults(func=cmd_bench)

    from .demo import add_parser as _add_demo_parser
    _add_demo_parser(sub)

    # The gate commands: the register, notifications, parties, declarations,
    # screening, the assembled file, and the one command that records a
    # determination. See cgate/cli_register.py.
    from .cli_register import add_parsers as _add_gate_parsers
    _add_gate_parsers(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    # Reviews quote UN contract text; a Windows console code page must not
    # be able to mangle it on the way to the terminal.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)
