# Hand-check: what the public corpus found, and what it cost to fix

`docs/public-tender-evaluation.md` is generated and counts only. This page is the
correctness record: a human read findings against their quoted source, decided
which were real, and the fixes that followed.

Corpus: `bench/public/evaluate.py fetch --rows 300 --os 0` → 134 real procurement
notices published for open international competition. First run 2026-10-21.

## Round 1 — the original 12 findings

First run raised **38 substantive findings**. The first 12, taken from the
documents that raised the most (so a worst-case-biased sample), were read against
their source:

| # | Notice | Check | Quoted source | Verdict |
|---|---|---|---|---|
| 1 | `OP00474905` | `value_conflict` | "USD 100" | **Bug.** Subject was "dollar, hundr, meticai, mzn, stat, thousand, unit" — a bag of unit words, not a quantity. |
| 2 | `OP00474905` | `disqualifying_rule_off_anchor` | "Late Proposals will be rejected." | **Artefact.** Anchored to "(front matter)" because a notice has no sections. |
| 3 | `OP00474905` | `single_point_obligation` | Proposal security MZN 32,000,000 / USD 500,000 | **Artefact.** Stated once, correctly, in a notice. |
| 4 | `OP00474905` | `single_point_obligation` | "The main qualification criteria are…" | **Artefact.** |
| 5 | `OP00476020` | `value_conflict` | "US$ 100" | **Bug.** Same bag-of-units subject: "dollar, fresh, hundr". |
| 6 | `OP00476020` | `disqualifying_rule_off_anchor` | "Late Bids will be rejected." | **Artefact.** |
| 7 | `OP00476020` | `single_point_obligation` | "…criteria are met and included in their bid…" | **Artefact.** |
| 8 | `OP00476020` | `single_point_obligation` | Bid security US$ 160,000 | **Artefact.** |
| 9 | `OP00476300` | `disqualifying_rule_off_anchor` | "Late Bids will be rejected." | **Artefact.** |
| 10 | `OP00476300` | `single_point_obligation` | "Bidders shall meet the following minimum qualification criteria…" | **Artefact.** |
| 11 | `OP00476300` | `single_point_obligation` | Bid security GEL 5,600,000 / 3,500,000 | **Artefact.** |
| 12 | `OP00459459` | `disqualifying_rule_off_anchor` | "Key Experts will not be evaluated at the shortlisting stage." | **Artefact.** |

**0 genuine defects. 2 extraction bugs. 10 artefacts of document class.**

Not one of the 12 invented text: every quote genuinely exists in its source. The
failure was never fabrication — it was **subject** and **anchor**.

## The four bugs, and the fixes

### 1. Unit vocabulary counted as subject identity

`subject_of` returned the stems around a figure, including the currency and
magnitude words. Two unrelated amounts in dollars therefore shared `usd`,
`dollar`, `thousand`, `unit`, were linked as "one quantity", and reported as a
contradiction whose subject was *made entirely of units* — which is itself
evidence the figures are not the same quantity.

**Fix:** `_UNIT_NOISE` in `spec.py` strips currency names, currency codes and
magnitude words from a subject. Time units are deliberately **not** in that set: 90
days and 120 days really are two answers to one question.

### 2. Clock times read as durations

`1600 hours (local time)` is 4pm. Read as a quantity it is a count of 1600 hours,
so a 4pm deadline in one notice and a 10am deadline in another became "two
different values for one quantity".

**Fix:** `_is_clock_time` rejects `hour`/`hours`/`hrs` when the figure is
introduced by at/to/from/between/until/by, qualified as local time, or is a
leading-zero four-digit time. `1600 hours of expert input` still counts.

### 3. Placement checks treated "no structure" as "wrong structure"

`disqualifying_rule_off_anchor` and `single_point_obligation` compare where a rule
sits against the places a document declares. A notice declares none, so every
paragraph was "filed away from the submission requirements" and every obligation
was "stated once". Two more sub-cases made it worse:

- a heading the tool does not recognise is classified `other`, and the notice's
  own title is unnumbered, so **the document title was being read as a section**;
- `REQUEST FOR EXPRESSIONS OF INTEREST (CONSULTING SERVICES)` was classified
  `scope` because the word "services" appears in its own title.

**Fix:** `UNPLACED_KINDS` (`"front"`, `"other"`, `None`) is not a place, and
`_wrapper_blocks` excludes the unnumbered heading that runs to the end of the
document. Where a check cannot judge, it returns a `did not run` advisory — the
same contract `R000` already used for cross-references — instead of a
high-severity finding.

### 4. "Check did not run" was counted as a finding

`R000` was modelled as a defect, so a document with no numbered provisions
contributed one "finding" that was really a statement of non-coverage. The
evaluation counts these separately now, and the report says so.

## Round 2 — after the fixes

| | Before | After |
|---|---|---|
| Substantive findings | 38 | **2** |
| Documents with a finding | 25 of 134 | **2 of 134** |
| `value_conflict` | 3 | 0 |
| `disqualifying_rule_off_anchor` | 21 | 0 |
| `single_point_obligation` | 12 | 0 |
| `external_reference` | 2 | **2** |

The two survivors are both `external_reference`: `Section V` and `Section 70`,
cited by notices that do not carry the sections they name. **Both are correct.**
The tool is right to say a reference points outside the document — it does.

Regression coverage: `tests/test_public_regressions.py`, 12 tests, each written
from the shape that produced the false positive, plus a positive control for every
check so a future "simplification" cannot mute it. Full suite: 396 passed.

## What this is and is not evidence for

It is evidence that the tool runs on 134 real documents from 50 countries without
an error, and that the findings it produces on them survive a human reading.

It is **not** an accuracy measurement. These notices have no expert-labelled
ground truth. Two findings that are correct is not "100% precision" — it is two
findings. The honest reading is narrower and still useful: on documents the tool
was *not* designed for, it now declines where it cannot judge and reports only
what it can ground.

The real limitation remains: **77 of 134 notices compiled no commitments at all**,
because a notice is a summary with no numbered clauses. A genuine accuracy claim
needs full solicitation documents. That is the next corpus, not this one.
