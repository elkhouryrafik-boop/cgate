"""cgate.demo — one command that exercises the whole pipeline on the bundled sample.

Everything here drives the same sub-commands a person would type, against the
invented tender in examples/. Nothing is mocked and nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sqlite3
import sys

from . import db

TENDER = os.path.join("examples", "blind-rfp", "blind-rfp-schools.docx")
PROPOSAL = os.path.join("examples", "proposal", "our-proposal.docx")
PURSUIT = "DEMO-1"


def _step(title: str) -> None:
    print(f"\n== {title}")


def _run(argv: list[str], *, expect: int | None = 0) -> int:
    """Run one cgate sub-command in-process, echoing it first."""
    from .cli import main

    print("$ cgate " + " ".join(argv))
    rc = main(argv)
    if expect is not None and rc != expect:
        print(f"demo: expected exit {expect}, got {rc}", file=sys.stderr)
        raise SystemExit(1)
    return rc


def cmd_demo(args: argparse.Namespace) -> int:
    for path in (TENDER, PROPOSAL):
        if not os.path.exists(path):
            print(f"demo: {path} not found; run from the repository root "
                  f"(python examples/make_sample.py --out examples builds it)",
                  file=sys.stderr)
            return 2

    out = args.out
    os.makedirs(out, exist_ok=True)
    store = os.path.join(out, "store")
    dbp = os.path.join(out, "register.db")
    if os.path.exists(dbp):
        os.remove(dbp)

    _step("1. review: the 15-item checklist against the tender")
    _run(["--store", store, "review", TENDER, "--out", out])

    _step("2. spec: compile the tender, then lint it against itself")
    _run(["spec", TENDER, "--out", out])

    _step("3. register: open a demo pursuit in a temporary append-only register")
    _run(["--store", store, "register", "init", "--db", dbp])
    _run(["notify", PURSUIT, "--db", dbp, "--client", "Aldermere Development Foundation",
          "--title", "Velmara schools rehabilitation (demo)", "--deadline", "2027-03-01",
          "--notified-by", "Demo BD Manager", "--tender-reference", "ADF/VLM/RFP-2026/117",
          "--jurisdiction", "XX"])

    _step("4. decide refuses a determination that no accountable person made")
    _run(["decide", PURSUIT, "clearance", "--db", dbp, "--reasons", "demo: automated pass",
          "--name", "system:auto", "--role", "CO", "--signature", "DEMO-SIG-0"], expect=3)
    _run(["decide", PURSUIT, "entry_closed", "--db", dbp, "--reasons", "demo: close early",
          "--name", "Demo Officer", "--role", "CO", "--signature", "DEMO-SIG-1"], expect=3)
    _run(["decide", PURSUIT, "clearance", "--db", dbp, "--reasons", "demo: reviewed the file",
          "--name", "Demo Officer", "--role", "CO", "--signature", "DEMO-SIG-2"])

    _step("5. the register refuses an edit (append-only)")
    conn = db.connect(dbp)
    try:
        conn.execute("UPDATE ledger SET actor = 'someone else'")
    except sqlite3.DatabaseError as exc:
        print(f"refused by SQLite: {exc}")
    else:
        print("demo: the ledger accepted an UPDATE", file=sys.stderr)
        return 1
    finally:
        conn.close()
    _run(["register", "verify", "--db", dbp])

    _step("6. bench: the extractor against the invented labelled fixtures")
    _run(["--store", store, "bench"])

    _step("7. submission: self-check of the invented proposal")
    _run(["--store", store, "submission", PROPOSAL, "--solicitation", TENDER, "--firm", "Example Engineering Ltd",
          "--db", dbp, "--pursuit", PURSUIT, "--out", out], expect=None)

    print(f"\ndemo complete. Outputs in {out}: review.md, spec.md, register.db")
    return 0


def add_parser(sub) -> None:
    d = sub.add_parser("demo", help="run the whole pipeline on the bundled invented tender")
    d.add_argument("--out", default=os.path.join("out", "demo"))
    d.set_defaults(func=cmd_demo)


# kept for tests that want the captured transcript
def capture(argv: list[str]) -> tuple[int, str]:
    from .cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()
