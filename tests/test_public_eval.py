"""The public-eval harness must never store contact personal data.

The public procurement feed carries `contact_name`, `contact_email`, `contact_phone_no`
and `contact_address`. Every one of those is a private individual's data even
though the notice itself is public. `bench/public/evaluate.py` keeps a fixed
whitelist of fields and never writes the rest; these tests hold that line, so a
later edit that "just stores the whole row" fails here instead of shipping.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_eval", ROOT / "bench" / "public" / "evaluate.py")
public_eval = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(public_eval)


def test_kept_fields_exclude_every_contact_field():
    kept = set(public_eval.KEEP)
    for forbidden in ("contact_name", "contact_email", "contact_phone_no", "contact_address"):
        assert forbidden not in kept, f"{forbidden} must never be kept"
    assert "notice_text" in kept


def test_fetch_writes_no_contact_data(tmp_path, monkeypatch):
    """Run cmd_fetch against a stub feed that is full of personal data."""
    poison = {
        "id": "OP0TEST01",
        "notice_type": "Request for Proposals",
        "noticedate": "20-Oct-2026",
        "notice_lang_name": "English",
        "project_ctry_name": "Testland",
        "bid_reference_no": "TEST/1/2026",
        "procurement_method_name": "Request for Proposals",
        "notice_text": "<p>" + ("Binding requirement text. " * 120) + "</p>",
        "contact_name": "Person Name Here",
        "contact_email": "person@example.org",
        "contact_phone_no": "+000 555 0100",
        "contact_address": "1 Secret Street, Somewhere",
    }
    payload = {"total": "1", "procnotices": [poison]}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(payload).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())

    out = tmp_path / "corpus"
    rc = public_eval.main(["fetch", "--rows", "1", "--out", str(out)])
    assert rc == 0

    blob = "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in out.rglob("*") if p.is_file()
    )
    for leak in ("Person Name Here", "person@example.org", "+000 555 0100", "1 Secret Street"):
        assert leak not in blob, f"contact data leaked into the corpus: {leak}"

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["notices"][0]["id"] == "OP0TEST01"
    assert "notice_text" not in manifest["notices"][0]


def test_strip_html_keeps_block_structure():
    raw = "<p>First para</p><table><tr><td>a</td><td>b</td></tr></table><p>Second</p>"
    text = public_eval.strip_html(raw)
    assert "First para" in text and "Second" in text
    assert "a\tb" in text, "table cells must survive as a tab-separated row"


def test_short_notices_are_skipped(tmp_path, monkeypatch):
    payload = {"total": "1", "procnotices": [{"id": "TINY", "notice_text": "<p>too short</p>"}]}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(payload).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    out = tmp_path / "c"
    rc = public_eval.main(["fetch", "--rows", "1", "--out", str(out)])
    assert rc == 1, "a fetch that keeps nothing should report failure"
    assert not (out / "text" / "TINY.md").exists()
