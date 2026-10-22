"""
cgate.bench — measure the extractor instead of asserting that it works.

Fixtures are invented general-conditions excerpts, written in the register
that real general conditions use and annotated by hand with the provisions a
reviewer would expect to be found. They are not tuned to make the tool look
good: the expectations are authored first, and the extractor is measured
against them.

Three numbers, and what each is for
-----------------------------------
RECALL         Expected provisions found. The number the tool is for. A miss
               here is a provision a reviewer would have had to catch by hand.

DROP RATE      Findings deleted by the grounding gate. For the rules extractor
               this must be 0.000 — rules quote the source by construction, so
               any drop means an offset bug in `extract.spans_for`, which
               `ground` was built to surface rather than let pass silently.

FALSE FIRES    Categories firing on a fixture that contains no such provision.
               Tracked per fixture with an explicit ceiling, because precision
               debt that nobody counts is precision debt that grows.

The negative fixture is commercial boilerplate — payment, liability, insurance.
It contains no integrity provision. Anything the extractor reports there is
noise a reviewer has to read past, and a reviewer who reads past enough noise
stops reading.
"""

from __future__ import annotations

import glob
import json
import os

from .extract import extract
from .ground import gate
from .ingest import ingest
from .segment import segment

from . import anchor as anchor_
from . import reading


def _canon(s: str) -> str:
    return " ".join(s.lower().split())


class FixtureResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.hits: list[dict] = []
        self.misses: list[dict] = []
        self.false_fires: list[str] = []
        self.max_false_fires = 0
        self.extracted = 0
        self.dropped = 0
        self.drop_reasons: dict[str, int] = {}
        self.modality_ok = 0
        self.modality_checked = 0

    @property
    def recall(self) -> float | None:
        n = len(self.hits) + len(self.misses)
        return len(self.hits) / n if n else None

    @property
    def drop_rate(self) -> float:
        return self.dropped / self.extracted if self.extracted else 0.0

    @property
    def passed(self) -> bool:
        return (
            not self.misses
            and self.dropped == 0
            and len(self.false_fires) <= self.max_false_fires
        )


def run_fixture(txt_path: str) -> FixtureResult:
    name = os.path.splitext(os.path.basename(txt_path))[0]
    res = FixtureResult(name)

    spec_path = os.path.splitext(txt_path)[0] + ".expected.json"
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    res.max_false_fires = int(spec.get("max_false_fires", 0))

    doc = ingest(txt_path)
    clauses = segment(doc)
    reqs = extract(doc, clauses)
    kept, report = gate(doc, reqs)

    res.extracted = len(reqs)
    res.dropped = report.dropped
    res.drop_reasons = dict(report.reasons)

    for want in spec.get("expect", []):
        cat, quote = want["category"], _canon(want["quote"])
        found = None
        for r in kept:
            if r.category == cat and quote in _canon(r.text):
                found = r
                break
        if found is None:
            res.misses.append(want)
            continue
        res.hits.append(want)
        if want.get("modality"):
            res.modality_checked += 1
            if found.modality.value == want["modality"]:
                res.modality_ok += 1

    forbidden = set(spec.get("forbid", []))
    fired = sorted({r.category for r in kept} & forbidden)
    res.false_fires = fired
    return res


def run_bench(fixtures_dir: str, store: str = ".cgate", verbose: bool = True) -> int:
    paths = sorted(glob.glob(os.path.join(fixtures_dir, "*.txt")))
    if not paths:
        print(f"no fixtures in {fixtures_dir}")
        return 2

    results = [run_fixture(p) for p in paths]

    if verbose:
        print(f"{'fixture':<24} {'recall':>8} {'found':>7} {'missed':>7} "
              f"{'drop':>7} {'false':>7}  ")
        print("-" * 66)
    all_hits = all_miss = 0
    total_extracted = total_dropped = 0
    for r in results:
        all_hits += len(r.hits)
        all_miss += len(r.misses)
        total_extracted += r.extracted
        total_dropped += r.dropped
        rec = f"{r.recall:.0%}" if r.recall is not None else "n/a"
        mark = "" if r.passed else "  FAIL"
        if verbose:
            print(f"{r.name:<24} {rec:>8} {len(r.hits):>7} {len(r.misses):>7} "
                  f"{r.drop_rate:>7.3f} {len(r.false_fires):>7}{mark}")

    overall = all_hits / (all_hits + all_miss) if (all_hits + all_miss) else 0.0
    drop = total_dropped / total_extracted if total_extracted else 0.0
    if verbose:
        print("-" * 66)
        print(f"{'OVERALL':<24} {overall:>8.0%} {all_hits:>7} {all_miss:>7} {drop:>7.3f}")
        print()

        for r in results:
            if r.misses:
                print(f"{r.name}: {len(r.misses)} missed")
                for m in r.misses:
                    print(f"  [{m['category']}] {m['quote'][:110]}")
                    if m.get("why"):
                        print(f"        {m['why']}")
                print()
            if len(r.false_fires) > r.max_false_fires:
                print(f"{r.name}: false fires {r.false_fires} "
                      f"(ceiling {r.max_false_fires})")
                print()
            if r.drop_reasons:
                print(f"{r.name}: grounding gate dropped {r.dropped} — {r.drop_reasons}")
                print("  Rules quote the source by construction. A non-zero drop rate "
                      "here is an offset bug in extract.spans_for, not a model problem.")
                print()

        checked = sum(r.modality_checked for r in results)
        ok = sum(r.modality_ok for r in results)
        if checked:
            print(f"Modality correct on {ok}/{checked} matched provisions "
                  f"({ok / checked:.0%}).")
            print()

    return 0 if all(r.passed for r in results) else 1


# ---------------------------------------------------------------------------
# Anchor bench — a number that moves
# ---------------------------------------------------------------------------
#
# Runs the real pipeline against a solicitation, then runs the anchor planner
# against a saved copy of that document's *Word-side* body text — the text as
# Word's own object model would return it, not the PDF extraction. The two
# differ (conversion drift, §4), which is the entire reason anchoring is hard
# and the entire reason this is worth measuring rather than asserting a
# threshold.
#
# `body_text_path` should be a genuine capture of `Body.text` from Word with
# the document open (the add-in's phase 2 work makes that capture trivial to
# add as a one-off debug action). Absent that capture, a PDF-ingest text dump
# is a usable stand-in for exercising the planner end-to-end, but it will not
# show the whitespace/hyphenation/ligature drift that real Word conversion
# introduces, so its anchor rate should be read as an upper bound, not a
# faithful figure, until it is replaced with a real capture.


def run_anchor_bench(
    doc_path: str, body_text_path: str, checklist_path: str, store: str = ".cgate",
) -> dict:
    from .pipeline import run_review

    result, _lexicon_version, _blocking = run_review([doc_path], checklist_path, None, store)
    item_status = {i.item_id: i.status for i in result.items}
    provisions = [reading.read(r, item_status) for r in result.requirements]

    with open(body_text_path, encoding="utf-8") as fh:
        body_text = fh.read()

    plan = anchor_.plan(
        [{"req_id": p["req_id"], "quote": p["quote"], "band": p["exposure"]} for p in provisions],
        body_text,
    )

    s = plan["stats"]
    print(f"anchor plan — {os.path.basename(doc_path)} against {os.path.basename(body_text_path)}")
    print(f"  {s['total']} provisions: {s['exact']} exact, {s['fragment']} fragment, "
          f"{s['ambiguous']} ambiguous, {s['failed']} failed")
    print(f"  anchor rate (exact+fragment / total): {s['anchor_rate']:.0%}")
    if s["total"] and s["anchor_rate"] < 0.90:
        print("  below the §4.2 acceptance line of 90% — see the per-provision "
              "'reason' field in the /api/anchor-plan response for why.")
    return plan
