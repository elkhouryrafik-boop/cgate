# Planted defects — the sealed list

`blind-rfp-schools.md` (and the `.docx` built from it by `examples/make_sample.py`)
is an invented tender: a fictional issuer (Aldermere Development Foundation), a
fictional country (the Republic of Velmara) and a fictional project. It was
written by an isolated agent that knew nothing about this tool, and that agent
planted six defects. The list was sealed until after the first run.

Read the README first and see what `cgate spec` reports on the document. Then
compare it with this list.

| # | Defect | Where | Check that catches it |
|---|---|---|---|
| 1 | Bid validity is 90 days on the cover table and 120 days in Section 8; the bid-security date (24 April 2027) only works with 120 | cover table, Section 8 | `value_conflict`, `date_arithmetic` (resolves to 120 days) |
| 2 | Minimum average turnover is USD 750,000 in Section 4.3 and USD 900,000 in Section 5 (h) | Sections 4.3, 5 | `value_conflict` |
| 3a | The payment schedule sums to 95 %, with no total row | Section 7.2 table | `table_sum` |
| 3b | Payment milestone 4 is triggered by a third party's act ("Award of the works contract by the Foundation"), not by anything the Consultant delivers | Section 7.2 table | `foreign_trigger` |
| 4 | The professional indemnity insurance requirement (USD 1,500,000) is stated exactly once, inside the Terms of Reference | Section 3.2, item 8 | `single_point_obligation` |
| 5 | A disqualifying rule (a financial proposal in another currency, or exclusive of taxes, is non-responsive) is buried in the evaluation section instead of the mandatory requirements | Section 6.4 | `disqualifying_rule_off_anchor` |
| 6 | The Team Leader must be conversant in Swahili, stated in a reporting paragraph rather than beside the key-personnel requirements | Section 3.3 | `personnel_requirement_off_anchor` |

`tests/test_blind_rfp.py` asserts all six against the quoted source text.

## The invented proposal

`examples/proposal/our-proposal.docx` is a bid from "Example Engineering Ltd"
answering the tender above, with three deliberate issues for the self-check:

| Issue | Where in the proposal | What `cgate submission` does with it |
|---|---|---|
| A wrong figure: bid security of USD 2,500 against the USD 5,000 the tender requires | Section 6 | Not machine-checked. The tool says plainly that it does not compare figures; a reviewer reads the passage |
| A missing annex reference: Annex B (Conflict of Interest Disclosure Form) is never mentioned | Section 7 | Not machine-checked; listed here so a human reviewer can find it |
| A commitment the tender never asked for: "guarantee zero delays" and "unlimited design revisions at no additional cost" | Section 3 | Query marks on the absolute claims (`claim_absolute`) |

A fourth, unplanned mark is useful: the subcontractor named in Section 4
(`Harbourline Surveys Ltd`) is reported as an undeclared party when the
pursuit's declared parties do not include it.
