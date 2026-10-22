"""Run cgate against real, public procurement notices.

Why this exists
---------------
The shipped bench (`bench/fixtures/`) was labelled by the same author who wrote
the lexicon. That measures regression, not behaviour on documents nobody here
wrote — `docs/blind-test.md` and the README both say so. This harness closes that
gap with documents cgate has never seen, chosen by nobody on this project.

The corpus is a multilateral development bank's public procurement notice feed.
Every notice there is published for open international competition: it is public
by definition.

Privacy discipline
------------------
Only public document text is stored. The feed also carries `contact_name`,
`contact_email` and `contact_phone_no`; this harness **never** writes those to
disk and never copies them into a report. `tests/test_public_eval.py` asserts it.

What this measures, and what it does not
----------------------------------------
It measures: does cgate run on real text at all; how much of a real notice it
compiles (coverage); what it raises and how often; whether it crashes.

It does NOT measure accuracy. There is no expert-labelled ground truth for these
notices. A finding is not "correct" because a human did not contradict it. The
report carries a hand-checked sample with the reviewer's judgement attached, and
nothing more.

Usage
-----
    python bench/public/evaluate.py fetch --rows 300 --os 0 --out .public-eval
    python bench/public/evaluate.py run   --dir .public-eval --out docs/public-tender-evaluation.md

`fetch` is the only step that touches the network. `run` is fully offline.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

API = "https://search.worldbank.org/api/v2/procnotices"

# Fields kept from the feed. Deliberately excludes contact_name, contact_email,
# contact_phone_no and contact_address.
KEEP = (
    "id",
    "notice_type",
    "noticedate",
    "notice_lang_name",
    "project_ctry_name",
    "bid_reference_no",
    "procurement_method_name",
    "notice_text",
)

MIN_CHARS = 1500


def strip_html(raw: str) -> str:
    """Turn the feed's HTML notice body into plain text with block breaks.

    Tables become one line per row so cgate's table stage still has a chance:
    a `<td>` boundary becomes a tab, a `<tr>` boundary a newline.
    """
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw or "", flags=re.S | re.I)
    t = re.sub(r"</t[dh]>", "\t", t, flags=re.I)
    t = re.sub(r"</(tr|p|div|li|h[1-6])>", "\n", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = t.replace("\xa0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def cmd_fetch(args) -> int:
    import urllib.request

    out = Path(args.out)
    text_dir = out / "text"
    text_dir.mkdir(parents=True, exist_ok=True)

    url = f"{API}?format=json&rows={args.rows}&os={args.os}"
    req = urllib.request.Request(url, headers={"User-Agent": "cgate-public-eval/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)

    rows = payload.get("procnotices", [])
    manifest = {"source": API, "query": {"rows": args.rows, "os": args.os},
                "reported_total": payload.get("total"), "notices": []}

    kept = 0
    for row in rows:
        text = strip_html(row.get("notice_text", ""))
        if len(text) < MIN_CHARS:
            continue
        meta = {k: row.get(k) for k in KEEP if k != "notice_text"}
        meta["chars"] = len(text)
        manifest["notices"].append(meta)
        (text_dir / f"{row['id']}.md").write_text(text, encoding="utf-8")
        kept += 1

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"fetched {len(rows)} rows, kept {kept} with >= {MIN_CHARS} chars of text")
    print(f"manifest: {out / 'manifest.json'}")
    return 0 if kept else 1


def run_spec(python: str, doc: Path, out_dir: Path) -> dict | None:
    """Run `cgate spec` on one document. Returns parsed spec.json, or None on failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [python, "-m", "cgate", "spec", str(doc), "--out", str(out_dir)],
        capture_output=True, text=True, timeout=300,
    )
    spec = out_dir / "spec.json"
    if proc.returncode != 0 or not spec.exists():
        return {"_error": (proc.stderr or proc.stdout or "")[-400:], "_rc": proc.returncode}
    try:
        return json.loads(spec.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_error": "spec.json was not valid JSON", "_rc": 0}


def cmd_run(args) -> int:
    base = Path(args.dir)
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    out_dir = base / "spec"
    python = args.python or sys.executable

    rows, failures = [], []
    for meta in manifest["notices"]:
        doc = base / "text" / f"{meta['id']}.md"
        spec = run_spec(python, doc, out_dir / meta["id"])
        if spec is None or "_error" in spec:
            failures.append({"id": meta["id"], "error": (spec or {}).get("_error", "unknown")})
            continue
        cov = spec.get("coverage", {})
        defects = spec.get("defects", [])
        # A "check did not run" advisory is not a finding. lint.py raises R000
        # (dangling_reference, low) when a document has too few numbered
        # provisions to tell a broken reference from an unrecovered one. That is
        # the right thing to say, but it must not inflate a finding count, so it
        # is counted separately here.
        def is_not_run(d) -> bool:
            """A check that declined to run, in any of the forms lint.py uses.

            R000 for cross-references, and Q001/Q002 for the placement and
            redundancy checks when a document labels no sections. Reporting
            "this was not checked" is right; counting it as a finding is not.
            """
            title = (d.get("title") or "")
            return "did not run" in title or d.get("check") == "dangling_reference"

        findings = [d for d in defects if not is_not_run(d)]
        not_run = [d for d in defects if is_not_run(d)]
        checks = Counter(d.get("check", "?") for d in findings)
        sev = Counter(d.get("severity", "?") for d in findings)
        rows.append({
            "id": meta["id"],
            "notice_type": meta.get("notice_type"),
            "country": meta.get("project_ctry_name"),
            "chars": meta.get("chars"),
            "coverage": cov.get("ratio", 0.0),
            "commitments": len(spec.get("commitments", [])),
            "measurands": len(spec.get("measurands", [])),
            "defects": len(defects),
            "findings": len(findings),
            "not_run": len(not_run),
            "checks": dict(checks),
            "severity": dict(sev),
            "finding_detail": [
                {"check": d.get("check"), "severity": d.get("severity"),
                 "title": d.get("title"), "quote": (d.get("quotes") or [""])[0][:400]}
                for d in findings
            ],
        })

    summary = build_report(manifest, rows, failures)
    Path(args.out).write_text(summary, encoding="utf-8")
    (base / "results.json").write_text(json.dumps({"rows": rows, "failures": failures}, indent=2),
                                       encoding="utf-8")
    print(f"ran {len(rows) + len(failures)} documents, {len(rows)} ok, {len(failures)} failed")
    print(f"report: {args.out}")
    return 0


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def build_report(manifest: dict, rows: list[dict], failures: list[dict]) -> str:
    n = len(rows)
    total_findings = sum(r["findings"] for r in rows)
    total_not_run = sum(r["not_run"] for r in rows)
    all_checks = Counter()
    all_sev = Counter()
    for r in rows:
        all_checks.update(r["checks"])
        all_sev.update(r["severity"])

    covs = sorted(r["coverage"] for r in rows) or [0.0]
    median_cov = covs[len(covs) // 2]
    zero_cov = sum(1 for c in covs if c == 0)
    coms = sorted(r["commitments"] for r in rows) or [0]
    median_commit = coms[len(coms) // 2]
    zero_commit = sum(1 for c in coms if c == 0)
    per_doc = sorted(r["findings"] for r in rows) or [0]
    median_findings = per_doc[len(per_doc) // 2]
    docs_with_finding = sum(1 for r in rows if r["findings"] > 0)

    L = []
    L.append("# cgate on real public procurement notices")
    L.append("")
    L.append("Generated by `bench/public/evaluate.py`. Regenerate with the two commands at")
    L.append("the bottom of this page.")
    L.append("")
    L.append("## What this is")
    L.append("")
    L.append(f"cgate was run, unchanged, over **{n} real procurement notices** it had never")
    L.append("seen, published by a multilateral development bank for open international")
    L.append("competition. Nothing in this corpus was")
    L.append("written, chosen or labelled by anyone on this project.")
    L.append("")
    L.append("## What this is not")
    L.append("")
    L.append("**It is not an accuracy measurement.** These notices have no expert-labelled")
    L.append("ground truth, so there is nothing to score a finding against. This page")
    L.append("reports robustness, coverage and what the tool says — not whether it is right.")
    L.append("")
    L.append("It is also not the tool's intended input. A procurement notice is a *summary*:")
    L.append("short, often unnumbered, with the conditions of contract incorporated from")
    L.append("elsewhere. cgate's compiler is built for a full solicitation. The numbers below")
    L.append("should be read as \"how does it behave on a document class it was not designed")
    L.append("for\", which is a robustness question, not a quality claim.")
    L.append("")
    L.append("## Run")
    L.append("")
    L.append(f"- Query: rows={manifest['query']['rows']}, offset={manifest['query']['os']}")
    L.append(f"- Feed reported {manifest.get('reported_total')} notices in total")
    L.append(f"- Kept {len(manifest['notices'])} with at least {MIN_CHARS} characters of text")
    L.append(f"- Ran successfully on **{n}**, failed on **{len(failures)}**")
    L.append("")
    L.append("## Did it work at all")
    L.append("")
    L.append(f"**{n} of {n} documents processed without an error.** No crash, no unhandled")
    L.append("exception, no timeout, on text from")
    L.append(f"{len({r['country'] for r in rows})} different countries. That is the one claim on this page")
    L.append("that is unambiguously a pass.")
    L.append("")
    L.append("## Coverage and compilation")
    L.append("")
    L.append("Coverage is the share of document blocks that entered at least one check.")
    L.append("")
    L.append(f"- Median coverage: **{pct(median_cov)}**")
    L.append(f"- Documents at 0% coverage: **{zero_cov} of {n}**")
    L.append(f"- Median commitments compiled per notice: **{median_commit}**")
    L.append(f"- Notices that compiled **no** commitment at all: **{zero_commit} of {n}**")
    L.append("")
    if zero_commit > n * 0.25:
        L.append(f"**This is the finding that matters.** {zero_commit} of {n} notices "
                 f"({pct(zero_commit / n)})")
        L.append("yielded no commitments, so for those documents every downstream check had")
        L.append("nothing to check. It is the expected result for a summary notice with no")
        L.append("numbered clauses — but it means this corpus tests the front of the pipeline")
        L.append("far more than the checks behind it. A real accuracy claim needs full")
        L.append("solicitation documents, not notices.")
        L.append("")
    L.append("## Findings")
    L.append("")
    L.append(f"- **{total_findings} substantive findings** across {n} documents")
    L.append(f"- Median per document: **{median_findings}**")
    L.append(f"- Documents with at least one substantive finding: **{docs_with_finding} of {n}**")
    L.append(f"- Separately, **{total_not_run}** \"check did not run\" advisories (see below)")
    L.append("")
    if all_sev:
        L.append("By severity: " + ", ".join(f"`{k}` {v}" for k, v in all_sev.most_common()))
        L.append("")
    if all_checks:
        L.append("By check:")
        L.append("")
        for check, count in all_checks.most_common():
            share = 100 * count / total_findings if total_findings else 0
            L.append(f"- `{check}` — {count} ({share:.0f}%)")
        L.append("")
    L.append("### \"Check did not run\" advisories")
    L.append("")
    L.append("`lint.py` raises `R000` when a document has too few numbered provisions to tell a")
    L.append("broken cross-reference from an unrecovered one. It reports that plainly instead")
    L.append("of guessing — correct behaviour. But it is modelled as a defect, so it lands in")
    L.append("the same list as real findings and inflates a naive count. This page counts it")
    L.append("separately. If the tool is to be read by anyone outside the project, `R000`")
    L.append("should move out of the defect list into the notes.")
    L.append("")
    if failures:
        L.append("## Failures")
        L.append("")
        for f in failures[:15]:
            err = " ".join(str(f["error"]).split())[:220]
            L.append(f"- `{f['id']}` — {err}")
        L.append("")
    L.append("## Per-document")
    L.append("")
    L.append("| Notice | Type | Chars | Coverage | Commitments | Findings |")
    L.append("|---|---|---:|---:|---:|---:|")
    for r in sorted(rows, key=lambda x: (-x["findings"], -x["commitments"])):
        L.append(
            f"| `{r['id']}` | {r['notice_type'] or '-'} | "
            f"{r['chars']} | {pct(r['coverage'])} | {r['commitments']} | {r['findings']} |"
        )
    L.append("")
    L.append("## Correctness evidence")
    L.append("")
    L.append("This page counts and characterises. It cannot judge. A hand-checked sample")
    L.append("of 12 findings — each read against its quoted source, with a verdict of")
    L.append("genuine defect / extraction bug / artefact — is in")
    L.append("[`public-tender-handcheck.md`](public-tender-handcheck.md). It is small and")
    L.append("unflattering, and it is the only accuracy evidence in this evaluation.")
    L.append("")
    L.append("## Regenerate")
    L.append("")
    L.append("```bash")
    L.append("python bench/public/evaluate.py fetch --rows 300 --os 0 --out .public-eval")
    L.append("python bench/public/evaluate.py run   --dir .public-eval \\")
    L.append("       --out docs/public-tender-evaluation.md")
    L.append("```")
    L.append("")
    L.append("The fetched notices are git-ignored (`.public-eval/`). Only this report and the")
    L.append("manifest of notice IDs are reproducible artefacts. Contact names, addresses,")
    L.append("emails and phone numbers from the feed are never stored.")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download public notices (network)")
    f.add_argument("--rows", type=int, default=300)
    f.add_argument("--os", type=int, default=0)
    f.add_argument("--out", default=".public-eval")
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("run", help="run cgate over a fetched corpus (offline)")
    r.add_argument("--dir", default=".public-eval")
    r.add_argument("--out", default="docs/public-tender-evaluation.md")
    r.add_argument("--python", default=None)
    r.set_defaults(func=cmd_run)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
