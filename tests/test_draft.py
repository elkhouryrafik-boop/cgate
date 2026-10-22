"""The local drafting model, and the fence around it.

Most of these tests are about what `cgate.draft` is NOT allowed to do. The tool's
central claim is that no language model produces a finding, a status or a
determination; adding a model to the build is exactly when that claim needs
tests rather than a paragraph in a README.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cgate import draft  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARK = {
    "mark_id": "m1",
    "kind": "query",
    "heading": "Support this: 'ISO 37001'",
    "detail": "A certification claimed without its certificate reference.",
    "evidence": "Matched a claim pattern in the submission's own text.",
    "quote": "We are ISO 37001 certified and fully compliant with all applicable standards.",
}


class FakeResponse:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_generate(monkeypatch, text):
    monkeypatch.setattr(draft.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse({"response": text}))


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------


def test_the_review_path_does_not_import_the_model():
    """The reproducibility promise rests on extraction being deterministic. If
    `pipeline`, `adjudicate` or `extract` ever import `draft`, a signed review
    stops being reproducible and the README's "No language model is involved"
    becomes false. This is the test that notices."""
    for module in ("pipeline", "adjudicate", "extract", "ground", "report",
                   "resolve", "screen", "gate", "db", "lint", "spec"):
        src = (ROOT / "cgate" / (module + ".py")).read_text(encoding="utf-8")
        assert "draft" not in src.replace("drafted", "").replace("draft_", ""), \
            f"cgate/{module}.py references the drafting model"


def test_a_non_loopback_endpoint_is_refused_not_warned():
    """One environment variable pointing at a hosted model would quietly turn a
    local tool into a cloud one, and the privacy answer -- the documents go
    nowhere -- would silently become untrue."""
    os.environ["CGATE_OLLAMA_URL"] = "https://api.example.com"
    try:
        with pytest.raises(draft.DraftUnavailable) as e:
            draft.draft_fix(MARK)
        assert not e.value.retryable
        assert "loopback" in str(e.value)

        ok, why = draft.available()
        assert not ok and "loopback" in why
    finally:
        del os.environ["CGATE_OLLAMA_URL"]


def test_a_draft_asserting_a_verdict_is_discarded(monkeypatch):
    """The one sentence this system may never show a reviewer is the one a model
    reaches for first: that the passage is now fine."""
    for verdict in ("This passage is now fully compliant with the requirement.",
                    "The claim has been cleared and satisfied.",
                    "The requirement is satisfied by the paragraph above."):
        fake_generate(monkeypatch, verdict)
        with pytest.raises(draft.DraftUnavailable) as e:
            draft.draft_fix(MARK)
        assert "verdict" in str(e.value)


def test_a_clean_draft_survives_the_gate(monkeypatch):
    # Names a certificate without asserting a verdict about it. An earlier gate
    # rejected this sentence for containing "certif", which is precisely the
    # honest phrasing the feature exists to produce.
    fake_generate(monkeypatch, "We hold an anti-bribery management system aligned to "
                               "ISO 37001; the certificate reference is available on request.")
    d = draft.draft_fix(MARK)
    assert d.source == "model"
    assert d.mark_id == "m1"
    assert "not a finding" in d.as_dict()["disclaimer"]


def test_declining_to_invent_is_not_an_error_worth_retrying(monkeypatch):
    """Asked to support a claim it cannot support, the correct model behaviour is
    to refuse. That must not read to the pane as a transient failure."""
    fake_generate(monkeypatch, "CANNOT DRAFT")
    with pytest.raises(draft.DraftUnavailable) as e:
        draft.draft_fix(MARK)
    assert not e.value.retryable


def test_a_mark_with_no_passage_cannot_be_drafted(monkeypatch):
    """A shared document author belongs to the file, not to any sentence in it.
    There is nothing to redraft and inventing an anchor would be a fabrication."""
    with pytest.raises(draft.DraftUnavailable) as e:
        draft.draft_fix({"mark_id": "m2", "quote": "   "})
    assert not e.value.retryable


def test_the_models_reasoning_never_reaches_the_document(monkeypatch):
    """qwen3 emits a <think> block by default. It is reasoning, not the answer."""
    fake_generate(monkeypatch, "<think>The user wants me to soften this claim. "
                               "I should avoid inventing a certificate.</think>\n"
                               "We align our anti-bribery controls to ISO 37001.")
    d = draft.draft_fix(MARK)
    assert "<think>" not in d.proposed
    assert "The user wants" not in d.proposed
    assert d.proposed.startswith("We align")


def test_a_wrapped_or_fenced_draft_is_unwrapped(monkeypatch):
    fake_generate(monkeypatch, '```\n"We align our controls to ISO 37001."\n```')
    d = draft.draft_fix(MARK)
    assert d.proposed == "We align our controls to ISO 37001."


def test_a_draft_much_longer_than_the_passage_is_flagged(monkeypatch):
    fake_generate(monkeypatch, "We align our controls to the standard. " * 40)
    d = draft.draft_fix(MARK)
    assert any("longer" in w for w in d.warnings)


# ---------------------------------------------------------------------------
# The fabrication backstop
# ---------------------------------------------------------------------------


def test_an_invented_reference_number_is_discarded(monkeypatch):
    """Observed, not hypothetical. Told in the system prompt to invent nothing and
    asked to support an ISO 37001 claim, qwen3:8b answered "Our firm is ISO 37001
    certified, reference number XYZ123456."

    A fabricated certificate number, in a document going to a client, from a
    system whose whole purpose is that this firm can be trusted. The prompt did
    not stop it; this does."""
    fake_generate(monkeypatch, "Our firm is ISO 37001 certified, reference number XYZ123456.")
    with pytest.raises(draft.DraftUnavailable) as e:
        draft.draft_fix({"mark_id": "m1", "quote": "Our firm is ISO 37001 certified."})
    assert "XYZ123456" in str(e.value)


def test_an_invented_date_is_discarded(monkeypatch):
    fake_generate(monkeypatch, "Delivered in 2019 and 2021.")
    with pytest.raises(draft.DraftUnavailable):
        draft.draft_fix({"mark_id": "m1", "quote": "Delivered in 2019."})


@pytest.mark.parametrize("original,proposed", [
    # Carrying the firm's own figures across is the point, not a fabrication.
    ("Our firm is ISO 37001 certified.",
     "Our firm holds an anti-bribery management system aligned to ISO 37001."),
    ("We employ 12 engineers.", "We employ 12 engineers in the region."),
    ("Contract 4471902 refers.", "Contract 4471902 refers to the works."),
    # Deleting an unsupported claim is the fix we actually want.
    ("We are the leading firm.", "We are a consulting firm."),
])
def test_honest_redrafting_is_not_flagged(original, proposed):
    assert draft.fabrications(original, proposed) == []


def test_two_digit_numbers_are_not_treated_as_references():
    """A guard noisy enough to be switched off protects nothing. "12 staff" is a
    fact the model can reasonably carry; a certificate number is not."""
    assert draft.fabrications("We have staff.", "We have 12 staff.") == []
    assert draft.fabrications("We have staff.", "We have 1200 staff.") == ["1200"]
