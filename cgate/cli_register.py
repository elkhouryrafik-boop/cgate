"""
cgate.cli_register — the commands that operate the gate, not just the reviewer.

`cgate review` reads one document. These commands run the pre-submission
compliance gate of Document SOP-3.10 as a process with a register behind
it:

    cgate notify   P-1 ...         record the notification, start the clocks
    cgate party    P-1 ...         name a subcontractor, partner or agent
    cgate declare  P-1 ...         file a signed counterparty declaration
    cgate attend   P-1 ...         record who was at a site visit
    cgate screen   "A Firm"        screen a name against debarment sources
    cgate conflict ours.docx theirs.docx    shared-content detection
    cgate file     P-1 tender.pdf  assemble the decision-ready file
    cgate decide   P-1 clearance   record a determination, with a signature
    cgate register verify          recompute the ledger hash chain

The split is deliberate. Every command above writes evidence or reads it back.
Exactly one of them — `decide` — records a determination, and it is the only one
that demands a person's name, role and signature reference before it will run.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys

from . import db, registry, screen, workflow
from .report import render_gate

# `gate` and `conflict` are imported inside the commands that need them.
# They pull in numpy and Pillow for shared-content detection, and a person
# running `cgate review` or `cgate register verify` should not need either.

DEFAULT_DB = "register.db"


def _conn(args):
    return db.connect(args.db)


def _holidays(args) -> frozenset:
    """Public holidays, read from a file of ISO dates, one per line.

    Absent by default and reported as absent, because the firm operates across
    calendars this tool is not entitled to guess at.
    """
    path = getattr(args, "holidays", None)
    if not path:
        return frozenset()
    with open(path, encoding="utf-8") as fh:
        return frozenset(
            _dt.date.fromisoformat(line.strip())
            for line in fh
            if line.strip() and not line.startswith("#")
        )


# ---------------------------------------------------------------------------


def cmd_register(args) -> int:
    conn = _conn(args)
    if args.action == "init":
        print(f"register ready at {args.db}")
        return 0

    if args.action == "verify":
        report = db.verify_chain(conn)
        print(report)
        return 0 if report.ok else 1

    rows = db.register(conn)
    if not rows:
        print("register is empty")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for r in rows:
        outcome = r["outcome"] or "no determination recorded"
        print(f"{r['pursuit_id']}  {r['notification_received'][:10]}  "
              f"{r['client']}  {r['submission']}")
        print(f"    {outcome}"
              + (f" by {r['decided_by']} ({r['actor_role']}) "
                 f"[{r['signature_ref']}]" if r["outcome"] else ""))
    return 0


def cmd_notify(args) -> int:
    """Record the notification 3.10 §4.1 requires before a pursuit is worked on."""
    conn = _conn(args)
    client = db.upsert_entity(conn, args.client, "authority",
                              jurisdiction=args.client_jurisdiction,
                              created_by=args.notified_by)
    try:
        db.open_pursuit(
            conn, args.pursuit, client, args.title,
            submission_deadline=args.deadline, notified_by=args.notified_by,
            tender_reference=args.tender_reference, jurisdiction=args.jurisdiction,
            estimated_value=args.value, currency=args.currency,
            scope_category=args.scope, review_track=args.track,
            fast_track_approved_by=args.fast_track_approved_by,
            fast_track_approved_at=args.fast_track_approved_at,
        )
    except Exception as exc:                                  # noqa: BLE001
        if args.track == "fast_track" and "CHECK" in str(exc).upper():
            print("refusing to open a fast-track pursuit with no recorded ECC Chair "
                  "approval. 3.10 §7.1 prohibits fast-track being used to cure a "
                  "late notification, so the approval must exist before the track "
                  "is set, not after.", file=sys.stderr)
            return 3
        print(f"could not open pursuit: {exc}", file=sys.stderr)
        return 2

    print(f"{args.pursuit} notified. Clocks started.")
    print(workflow.summarise(workflow.clocks(conn, args.pursuit,
                                             holidays=_holidays(args))))
    return 0


def cmd_party(args) -> int:
    conn = _conn(args)
    eid = db.upsert_entity(conn, args.name, args.kind,
                           jurisdiction=args.jurisdiction,
                           aliases=args.alias, created_by=args.recorded_by)
    db.add_party(conn, args.pursuit, eid, args.role)
    print(f"{args.name} named on {args.pursuit} as {args.role} ({eid})")

    cols = registry.collisions_for(conn, args.pursuit, eid, args.role)
    if cols:
        print(f"\n{len(cols)} live cross-pursuit collision(s) — for CO determination, "
              "not a finding:")
        for c in cols:
            print(f"  {c.severity}: also on {c.other_pursuit_id} "
                  f"({c.other_pursuit_title}, client {c.other_client}) as {c.role_there}")
    if args.role in registry.DECLARABLE_ROLES:
        print(f"\nA signed counterparty declaration is required before submission. "
              f"File it with:\n  cgate declare {args.pursuit} --entity {eid} "
              f"--role {args.role} --ref DECL-... --signatory \"Name\"")
    return 0


def cmd_declare(args) -> int:
    conn = _conn(args)
    try:
        registry.sign_declaration(conn, args.pursuit, args.entity, args.role,
                                  args.ref, signatory=args.signatory,
                                  signed_at=args.signed_at)
    except ValueError as exc:
        print(f"refusing to file the declaration: {exc}", file=sys.stderr)
        return 2
    print(f"declaration {args.ref} filed for {args.entity} on {args.pursuit}, "
          f"signed by {args.signatory}")

    outstanding = registry.blocking(registry.standing(conn, args.pursuit))
    if outstanding:
        print(f"\n{len(outstanding)} party/parties still without a signed declaration:")
        for p in outstanding:
            print(f"  {p.legal_name} ({p.role}) — {p.declaration_state}")
    return 0


def cmd_attend(args) -> int:
    conn = _conn(args)
    entity = None
    if args.entity:
        entity = args.entity
    elif args.resolve:
        candidates = db.resolve_name(conn, args.attendee)
        if len(candidates) == 1:
            entity = candidates[0]["id"]
        elif len(candidates) > 1:
            print(f"{args.attendee!r} matches {len(candidates)} registry entities; "
                  "name the one you mean with --entity:", file=sys.stderr)
            for c in candidates:
                print(f"  {c['id']}  {c['legal_name']} "
                      f"({c['jurisdiction'] or 'no jurisdiction'})", file=sys.stderr)
            return 2
    db.record_attendance(conn, args.pursuit, args.kind, args.occurred_at,
                         attendee_name=args.attendee, attendee_entity=entity,
                         recorded_by=args.recorded_by, location=args.location,
                         source=args.source)
    print(f"attendance recorded: {args.attendee} at {args.kind} on {args.occurred_at}"
          + (f" (registry {entity})" if entity else " (not resolved to the registry)"))
    return 0


def cmd_screen(args) -> int:
    cfg = screen.ScreeningConfig.from_env(
        base_url=args.url, dataset=args.dataset, snapshot=args.snapshot)
    results = screen.screen_many(
        [{"name": n, "kind": args.kind, "country": args.country} for n in args.name],
        config=cfg)
    print(f"screened {len(results)} subject(s) against {cfg.source}")
    print(screen.summarise(results))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(screen.evidence(results), fh, indent=2, ensure_ascii=False)
        print(f"\nevidence written to {args.out}")

    # A run in which any subject failed to screen is not a successful run.
    return 1 if any(not r.ok for r in results) else 0


def cmd_conflict(args) -> int:
    from . import conflict

    result = conflict.scan(args.submission, args.material)
    if args.json:
        print(result.to_json())
    else:
        print(f"{os.path.basename(result.doc_a)} vs {os.path.basename(result.doc_b)}")
        if result.errors:
            for e in result.errors:
                print(f"  NOT SCANNED — {e['error_type']}: {e['message']}")
        else:
            print(f"  estimated text overlap: {result.text_similarity:.2%}")
            print(f"  exact passage matches:  {len(result.exact_matches)}")
            print(f"  image matches:          {len(result.image_matches)}")
            print(f"  shared properties:      {len(result.metadata_matches)}")
            for m in result.metadata_matches:
                print(f"    {m['field']}: {m['value']} ({m['weight']})")
        if result.triggers:
            print(f"  triggers: {', '.join(result.triggers)}")
    return 1 if result.triggers else 0


def cmd_file(args) -> int:
    """Assemble the decision-ready file for a pursuit."""
    from . import gate

    conn = _conn(args)
    cfg = screen.ScreeningConfig.from_env(
        base_url=args.url, dataset=args.dataset, snapshot=args.snapshot)

    attestations = {}
    if args.attestations:
        with open(args.attestations, encoding="utf-8") as fh:
            attestations = {str(k): v for k, v in json.load(fh).items()}

    try:
        gf = gate.assemble(
            conn, args.pursuit, args.solicitation,
            checklist_path=args.checklist, firm_name=args.firm,
            firm_aliases=args.firm_alias, submission_path=args.submission,
            third_party_material=args.material, screening_config=cfg,
            attestations=attestations, source_url=args.source_url,
            holidays=_holidays(args), as_of=args.as_of, store=args.store,
        )
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    md_path = os.path.join(args.out, f"{args.pursuit}-compliance-file.md")
    json_path = os.path.join(args.out, f"{args.pursuit}-review.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_gate(gf))
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(gf.review.to_json())

    if args.record:
        db.record_review(conn, args.pursuit, gf.review)
        print(f"{len(gf.review.items)} check results written to the register")

    counts: dict[str, int] = {}
    for item in gf.review.items:
        counts[item.status.value] = counts.get(item.status.value, 0) + 1
    print(f"{args.pursuit}: " + ", ".join(f"{n} {k.replace('_', ' ')}"
                                          for k, n in sorted(counts.items()) if n))

    reasons = gate.blocking_reasons(gf)
    if reasons:
        print(f"\n{len(reasons)} thing(s) standing in the way:")
        for r in reasons[:10]:
            print(f"  - {r}")
        if len(reasons) > 10:
            print(f"  ... and {len(reasons) - 10} more, in the file")
    print(f"\nwritten: {md_path}")
    print(f"         {json_path}")
    print("\nThis is not a clearance. A named human decides.")
    return 0


def cmd_decide(args) -> int:
    """Record a determination. The only command that changes a pursuit's outcome."""
    conn = _conn(args)
    try:
        workflow.record(
            conn, args.pursuit, args.decision, args.reasons,
            actor_name=args.name, actor_role=args.role,
            signature_ref=args.signature, conditions=args.conditions,
            decided_at=args.decided_at,
        )
    except (workflow.WorkflowError, db.DecisionError) as exc:
        print(f"refusing to record: {exc}", file=sys.stderr)
        return 3

    print(f"{args.pursuit}: {args.decision} recorded by {args.name} "
          f"({args.role}), signature {args.signature}")
    print(f"state is now {workflow.state_of(conn, args.pursuit)}")
    chain = db.verify_chain(conn)
    if not chain.ok:
        print(f"WARNING: {chain}", file=sys.stderr)
        return 1
    return 0


def cmd_clocks(args) -> int:
    conn = _conn(args)
    try:
        c = workflow.clocks(conn, args.pursuit, as_of=args.as_of,
                            holidays=_holidays(args))
    except workflow.WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(args.pursuit)
    print(workflow.summarise(c))
    return 1 if c.breaches else 0


def cmd_submission(args) -> int:
    """Mark up the firm's own outgoing submission."""
    from . import submission as sub
    from .report import render_marks

    staff = None
    if args.register:
        with open(args.register, encoding="utf-8") as fh:
            staff = json.load(fh)
        if not isinstance(staff, list):
            print("staff register must be a JSON list of {name, status, role}",
                  file=sys.stderr)
            return 2

    reqs = solicitation_id = None
    if args.solicitation:
        from .pipeline import run_review
        result, _lex, _blk = run_review(
            [args.solicitation], args.checklist, None, args.store)
        reqs = result.requirements
        solicitation_id = result.doc_id

    declared = None
    if args.pursuit:
        conn = db.connect(args.db)
        declared = [p.legal_name for p in registry.standing(conn, args.pursuit)]
    elif args.party:
        declared = args.party

    review = sub.review_submission(
        args.submission,
        solicitation_requirements=reqs,
        solicitation_id=solicitation_id,
        staff_register=staff,
        third_party_material=args.material,
        declared_parties=declared,
        firm_names=([args.firm] + args.firm_alias) if args.firm else None,
    )

    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.submission))[0]
    md_path = os.path.join(args.out, f"{stem}-marks.md")
    json_path = os.path.join(args.out, f"{stem}-marks.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_marks(review))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(review.to_dict(), fh, indent=2, ensure_ascii=False)

    counts = review.counts()
    print(f"{os.path.basename(args.submission)}: "
          f"{counts['correction']} correction(s), {counts['query']} query/queries, "
          f"{counts['commendation']} verified")
    ran = len(review.rubric.checks_run)
    print(f"  {ran} of 5 checks ran"
          + (f"; {len(review.rubric.checks_skipped)} did not" if review.rubric.checks_skipped else ""))
    for sk in review.rubric.checks_skipped:
        print(f"    - {sk.rule}: needs {sk.needs}")
    for m in review.by_kind(sub.CORRECTION)[:8]:
        print(f"  {sub.MARK_GLYPH[m.kind]} {m.heading}")
    print(f"\nwritten: {md_path}")
    print(f"         {json_path}")
    return 1 if counts["correction"] else 0


# ---------------------------------------------------------------------------


def add_parsers(sub) -> None:
    """Attach the gate commands to the top-level parser."""

    def _db(p):
        p.add_argument("--db", default=DEFAULT_DB,
                       help=f"the compliance register (default: {DEFAULT_DB})")
        return p

    r = _db(sub.add_parser("register", help="the Compliance Review Register (3.10 §10)"))
    r.add_argument("action", choices=["init", "show", "verify"], nargs="?",
                   default="show")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_register)

    n = _db(sub.add_parser("notify", help="record a notification and start the clocks"))
    n.add_argument("pursuit")
    n.add_argument("--client", required=True)
    n.add_argument("--title", required=True)
    n.add_argument("--deadline", required=True, metavar="YYYY-MM-DD")
    n.add_argument("--notified-by", required=True, help="the BD Manager notifying")
    n.add_argument("--tender-reference", default=None)
    n.add_argument("--jurisdiction", default=None,
                   help="ISO 3166-1 alpha-2 place of performance (checklist item 5)")
    n.add_argument("--client-jurisdiction", default=None)
    n.add_argument("--value", type=float, default=None)
    n.add_argument("--currency", default=None)
    n.add_argument("--scope", choices=["A", "B"], default="A", help="3.10 §2 scope")
    n.add_argument("--track", choices=["standard", "fast_track"], default="standard")
    n.add_argument("--fast-track-approved-by", default=None, help="the ECC Chair")
    n.add_argument("--fast-track-approved-at", default=None)
    n.add_argument("--holidays", default=None)
    n.set_defaults(func=cmd_notify)

    pa = _db(sub.add_parser("party", help="name a third party on a pursuit"))
    pa.add_argument("pursuit")
    pa.add_argument("--name", required=True)
    pa.add_argument("--role", required=True,
                    help="subcontractor, consortium_partner, agent, intermediary")
    pa.add_argument("--kind", choices=["firm", "individual", "authority"],
                    default="firm")
    pa.add_argument("--jurisdiction", default=None)
    pa.add_argument("--alias", action="append", default=[],
                    help="another name this party is known by; repeatable")
    pa.add_argument("--recorded-by", default="system:intake")
    pa.set_defaults(func=cmd_party)

    d = _db(sub.add_parser("declare", help="file a signed counterparty declaration"))
    d.add_argument("pursuit")
    d.add_argument("--entity", required=True, help="registry id from `cgate party`")
    d.add_argument("--role", required=True)
    d.add_argument("--ref", required=True, help="reference to the executed document")
    d.add_argument("--signatory", required=True,
                   help="the person who signed for the counterparty")
    d.add_argument("--signed-at", default=None)
    d.set_defaults(func=cmd_declare)

    at = _db(sub.add_parser("attend", help="record pre-bid attendance (item 6)"))
    at.add_argument("pursuit")
    at.add_argument("--kind", choices=["site_visit", "prebid_meeting",
                                       "clarification", "other"], required=True)
    at.add_argument("--occurred-at", required=True, metavar="YYYY-MM-DD")
    at.add_argument("--attendee", required=True)
    at.add_argument("--entity", default=None, help="registry id, if already known")
    at.add_argument("--resolve", action="store_true",
                    help="try to resolve the attendee name against the registry")
    at.add_argument("--location", default=None)
    at.add_argument("--source", default=None, help="sign-in sheet, minutes, photograph")
    at.add_argument("--recorded-by", required=True)
    at.set_defaults(func=cmd_attend)

    sc = sub.add_parser("screen", help="screen names against debarment sources")
    sc.add_argument("name", nargs="+")
    sc.add_argument("--kind", choices=["firm", "individual", "authority"],
                    default="firm")
    sc.add_argument("--country", default=None)
    sc.add_argument("--url", default=None,
                    help="screening API base URL, or a self-hosted yente instance")
    sc.add_argument("--dataset", default=None)
    sc.add_argument("--snapshot", default=None,
                    help="a local dataset file; screens offline")
    sc.add_argument("--out", default=None, help="write the evidence entry as JSON")
    sc.set_defaults(func=cmd_screen)

    cf = sub.add_parser("conflict", help="shared-content detection between documents")
    cf.add_argument("submission")
    cf.add_argument("material", help="material received from or shared with a party")
    cf.add_argument("--json", action="store_true")
    cf.set_defaults(func=cmd_conflict)

    fi = _db(sub.add_parser("file", help="assemble the decision-ready compliance file"))
    fi.add_argument("pursuit")
    fi.add_argument("solicitation", nargs="+")
    fi.add_argument("--firm", required=True,
                    help="the firm's own legal name, for screening (item 3)")
    fi.add_argument("--firm-alias", action="append", default=[])
    fi.add_argument("--checklist", default="config/checklist.example.yaml")
    fi.add_argument("--submission", default=None,
                    help="the outgoing submission, for shared-content detection")
    fi.add_argument("--material", action="append", default=[],
                    help="third-party material to compare against; repeatable")
    fi.add_argument("--attestations", default=None,
                    help="JSON of signed attestations for items 10, 11 and 15")
    fi.add_argument("--source-url", default=None,
                    help="where the solicitation was retrieved from (item 2)")
    fi.add_argument("--url", default=None)
    fi.add_argument("--dataset", default=None)
    fi.add_argument("--snapshot", default=None)
    fi.add_argument("--holidays", default=None)
    fi.add_argument("--as-of", default=None)
    fi.add_argument("--record", action="store_true",
                    help="write the check results into the register")
    fi.add_argument("--out", default="out")
    fi.set_defaults(func=cmd_file)

    de = _db(sub.add_parser("decide", help="record a determination (human only)"))
    de.add_argument("pursuit")
    de.add_argument("decision", choices=sorted(workflow.STATE_FOR_DECISION))
    de.add_argument("--reasons", required=True, help="3.10 §6.3 requires them")
    de.add_argument("--name", required=True, help="the named natural person deciding")
    de.add_argument("--role", required=True, choices=list(db.HUMAN_ROLES))
    de.add_argument("--signature", required=True,
                    help="reference to the executed record")
    de.add_argument("--conditions", default=None)
    de.add_argument("--decided-at", default=None)
    de.set_defaults(func=cmd_decide)

    cl = _db(sub.add_parser("clocks", help="the 3.10 clocks on a pursuit"))
    cl.add_argument("pursuit")
    cl.add_argument("--as-of", default=None)
    cl.add_argument("--holidays", default=None)
    cl.set_defaults(func=cmd_clocks)

    sm = _db(sub.add_parser("submission",
                            help="mark up the firm's own outgoing submission"))
    sm.add_argument("submission", help="our proposal or technical submission")
    sm.add_argument("--solicitation", default=None,
                    help="the client's tender, so coverage can be checked")
    sm.add_argument("--checklist", default="config/checklist.example.yaml")
    sm.add_argument("--register", default=None,
                    help="staff register JSON: [{name, status, role}] (items 13, 14)")
    sm.add_argument("--material", action="append", default=[],
                    help="material shared with a third party; repeatable")
    sm.add_argument("--pursuit", default=None,
                    help="read declared parties from the register for this pursuit")
    sm.add_argument("--party", action="append", default=[],
                    help="a declared party name, if not using --pursuit; repeatable")
    sm.add_argument("--firm", default=None,
                    help="our own legal name, so the proposal's letterhead is "
                         "not reported as an undeclared counterparty")
    sm.add_argument("--firm-alias", action="append", default=[])
    sm.add_argument("--out", default="out")
    sm.set_defaults(func=cmd_submission)

