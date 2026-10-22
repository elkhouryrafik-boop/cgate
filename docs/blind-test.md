# The blind test

Reading a document one clause at a time cannot find some defects, because they
are not properties of any single clause. The first version of this tool matched
clauses against a list of integrity subjects. Run against a tender written for
it by someone else, it found none of six planted defects. The compile and lint
stages exist because of that result.

## Setup

An isolated agent that knew nothing about the tool wrote a plausible request
for proposals and planted six defects. The list was sealed until after the
first run. The tender is invented: a fictional foundation, a fictional country
and a fictional school-rehabilitation project
(`examples/blind-rfp/blind-rfp-schools.md`). `examples/make_sample.py` builds the
`.docx` from it, with the payment schedule as a real Word table.

## Run it

```bash
python -m cgate spec examples/blind-rfp/blind-rfp-schools.docx --out out/spec
python -m pytest tests/test_blind_rfp.py -q      # 12 passed
```

The report is `out/spec/spec.md`. It raises 9 findings. Each quotes the source.

## What it found

| # | Planted defect | Check | Finding |
|---|---|---|---|
| 1 | Bid validity 90 days on the cover table, 120 days in section 8 | `value_conflict`, `date_arithmetic` | X001, A001: the stated end date only works with 120 days |
| 2 | Minimum turnover USD 750,000 in one section, 900,000 in another | `value_conflict` | X002 |
| 3a | Payment schedule sums to 95 % | `table_sum` | T001 |
| 3b | A milestone is triggered by the issuer's own act | `foreign_trigger` | F001 |
| 4 | Insurance cover required once, inside the terms of reference | `single_point_obligation` | S001 |
| 5 | A disqualifying currency rule buried in the evaluation section | `disqualifying_rule_off_anchor` | D001 |
| 6 | Team Leader language requirement in a reporting paragraph | `personnel_requirement_off_anchor` | P001 |

That is 8 findings for the six defects as numbered above. The ninth, S002, is
`single_point_obligation` on the same Team Leader sentence as defect 6, which is
also stated only once. Every finding is on a planted defect.

The answer key is [`examples/blind-rfp/PLANTED-DEFECTS.md`](../examples/blind-rfp/PLANTED-DEFECTS.md).

## Limits

One tender, one author, six defects. The checks were written after the first
failed run, so a pass here shows the checks work on this document, not that they
generalise. The tender is in English, born-digital, and has clean numbering.
