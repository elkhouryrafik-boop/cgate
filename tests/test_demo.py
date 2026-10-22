"""`cgate demo` runs the whole pipeline on the bundled invented tender."""

import os

from cgate.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLANTED_CHECKS = [
    "value_conflict",          # defects 1 and 2
    "date_arithmetic",         # defect 1
    "table_sum",               # defect 3a
    "foreign_trigger",         # defect 3b
    "single_point_obligation",  # defect 4
    "disqualifying_rule_off_anchor",       # defect 5
    "personnel_requirement_off_anchor",    # defect 6
]


def test_demo_writes_review_and_spec(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    out = tmp_path / "demo"
    assert main(["demo", "--out", str(out)]) == 0
    assert (out / "review.md").is_file()
    spec = (out / "spec.md").read_text(encoding="utf-8")
    for check in PLANTED_CHECKS:
        assert f"`{check}`" in spec
    assert (out / "register.db").is_file()
