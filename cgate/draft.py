"""A local model that drafts a replacement passage, and nothing else.

Everything else in this system is deterministic on purpose. `README.md` says
"No language model is involved" about extraction, and that sentence is load
bearing: it is what lets a signed review be reproduced years later, and it is
the answer to a reviewer's first question about the tool.

This module does not change that, and the boundary is worth stating precisely,
because the value of the claim depends on it:

    A model may propose WORDS for a human to accept.
    A model may never produce a FINDING, a STATUS, or a DECISION.

Nothing here is reachable from `pipeline.run_review`, from `adjudicate`, or from
any code path that sets a `model.Status`. `cgate review` and `cgate file` import
nothing from this file. What a draft becomes is a tracked insertion in Word,
which is a suggestion sitting in the document until a named person presses
Accept — and Reject removes it exactly, because Word wrote it as a revision.

Three guarantees hold the boundary:

1. **Local only.** The endpoint is Ollama on the loopback interface. Nothing
   leaves the machine, so the answer to "may documents go to a cloud service" is
   unchanged: the documents go nowhere. A non-loopback URL is refused, not
   warned about, because a configuration mistake here would silently turn a
   local tool into a cloud one.
2. **The word gate applies to the model too.** A draft that says the passage is
   now compliant, cleared, satisfied or approved is discarded rather than shown.
   The model is a better writer than it is a lawyer, and this is the sentence it
   is most likely to reach for.
3. **A draft is never evidence.** It carries `source: "model"` and the model's
   name everywhere it appears, and no draft is ever written to the feedback
   ledger, the register, or a check result.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"
TIMEOUT = 120

# The same list `tests/test_ui.py` holds the deterministic surfaces to, applied
# to generated prose. A model asked to fix a passage will very often announce
# that it has fixed it.
#
# Verdict words only, and the list is deliberately no wider than the one the
# rest of the system uses. A first draft of it also caught "approved" and
# "certif", which rejected *"the certificate reference is available on
# request"* -- the honest sentence this feature exists to produce. That is the
# failure mode `test_ui.py` warns about in as many words: a list that flags
# innocent text teaches the next person to widen the exemption, and the
# guarantee rots from there. Better to catch the four sentences that matter
# than to catch a hundred and be switched off.
FORBIDDEN = ("compliant", "non-compliant", "cleared", "clearance is",
             "passed", "satisfied")

SYSTEM = """You redraft one passage of a bidder's own proposal.

You are given the passage and one reviewer finding about it. Rewrite the passage
so it answers the finding.

Rules, all of them absolute:
- Return ONLY the replacement passage. No preamble, no explanation, no quotes
  around it, no markdown.
- Invent nothing. No new figures, dates, names, certifications, standards or
  track record.
- When the finding is that a claim is unsupported, DELETE the claim. Do not
  rephrase it, do not hedge it, and above all do not replace it with a
  different unsupported claim. "We have never had a project fail" must not
  become "we have an excellent record"; that is the same claim in a quieter
  voice, and it fails the same check. If removing the claim leaves the sentence
  empty, return the sentence without it.
- Superlatives and absolutes are claims: leading, best, largest, always,
  never, all, fully, unrivalled, guaranteed. Remove them rather than soften
  them, unless the passage itself carries the evidence for one.
- If you cannot fix it without inventing something, reply with exactly:
  CANNOT DRAFT
- Never state or imply that the passage is now compliant, cleared, approved,
  certified or satisfactory. You are drafting prose, not issuing a verdict.
- Keep the register and length close to the original.
"""


class DraftUnavailable(RuntimeError):
    """The local model could not be reached. Retryable; never a finding."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Draft:
    """A proposed replacement. Not evidence, and not a determination."""

    mark_id: str
    original: str
    proposed: str
    model: str
    source: str = "model"
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mark_id": self.mark_id,
            "original": self.original,
            "proposed": self.proposed,
            "model": self.model,
            "source": self.source,
            "warnings": list(self.warnings),
            # Repeated on the wire so no surface can render a draft without it.
            "disclaimer": (
                "Drafted by " + self.model + " on this machine. It is a suggestion, "
                "not a finding and not a determination. Nothing changes in the "
                "document until you accept the tracked change."
            ),
        }


def endpoint() -> str:
    return os.environ.get("CGATE_OLLAMA_URL", DEFAULT_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get("CGATE_DRAFT_MODEL", DEFAULT_MODEL)


def _assert_local(url: str) -> None:
    """Refuse anything that is not loopback.

    The whole tool's privacy answer is a fact about the code rather than a
    promise in a document. One environment variable pointing at a hosted
    endpoint would quietly make that false, so this raises instead of warning.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1", "[::1]"):
        raise DraftUnavailable(
            "refusing to send document text to " + (host or "an unnamed host") + ". "
            "Drafting runs against a model on this machine only; set "
            "CGATE_OLLAMA_URL to a loopback address.",
            retryable=False,
        )


def available() -> tuple[bool, str]:
    """Is a local model reachable, and is the configured one actually pulled?"""
    url = endpoint()
    try:
        _assert_local(url)
    except DraftUnavailable as e:
        return False, str(e)
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, "no local model host on " + url + " (" + type(e).__name__ + ")"

    have = {m.get("name", "") for m in tags.get("models", [])}
    want = model_name()
    if want not in have:
        return False, "model " + want + " is not pulled. Available: " + (
            ", ".join(sorted(have)) or "none")
    return True, want


#: A number of three digits or more, or a run of capitals and digits — the
#: shapes a certificate number, a registration, a standard reference, a date or
#: a monetary figure take. Two digits are excluded: "12 staff" is a fact the
#: model can reasonably carry across, and flagging it would make the guard noisy
#: enough to be switched off.
_IDENTIFIER = re.compile(r"\b\d[\d,.\-/]{2,}\b|\b[A-Z][A-Z0-9\-/]{3,}\b")


def fabrications(original: str, proposed: str) -> list:
    """Identifier-shaped tokens in the draft that were not in the passage.

    Told not to invent a source, qwen3 answered "Our firm is ISO 37001
    certified, reference number XYZ123456." That is the single most damaging
    thing this feature could produce: a fabricated certificate number, in a
    document going to a client, in a system whose entire purpose is that the
    firm can be trusted.

    A prompt cannot be relied on to prevent it — the instruction was already
    there and was ignored. This is the deterministic backstop. A model may
    rearrange, soften and delete the firm's words; it may not introduce a
    number, a reference or a code that nobody wrote.
    """
    have = original.upper()
    out = []
    for token in _IDENTIFIER.findall(proposed):
        if token.upper() not in have:
            out.append(token)
    return sorted(set(out))


def _clean(text: str) -> str:
    """Strip what a chat model adds even when told not to.

    qwen3 emits a <think> block by default; it is reasoning, not the answer, and
    it must never reach the document.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    text = re.sub(r"^```[a-z]*\n?|```$", "", text.strip(), flags=re.M).strip()
    # A model told to return only the passage still sometimes wraps it.
    if len(text) > 1 and text[0] in "\"“" and text[-1] in "\"”":
        text = text[1:-1].strip()
    return text


def draft_fix(mark: dict, *, model: Optional[str] = None) -> Draft:
    """Propose a replacement for one marked passage.

    `mark` is a `submission.Mark` as it reaches the pane: it carries the quote,
    the finding, and the evidence line. Raises `DraftUnavailable` when the model
    cannot be reached, which the caller reports as a retryable error rather than
    as an absence of suggestions.
    """
    quote = (mark.get("quote") or "").strip()
    if not quote:
        raise DraftUnavailable(
            "this mark has no quoted passage, so there is nothing to redraft. A "
            "shared document author, for instance, belongs to the file rather "
            "than to any sentence in it.",
            retryable=False,
        )

    url = endpoint()
    _assert_local(url)
    name = model or model_name()

    prompt = (
        "PASSAGE FROM OUR PROPOSAL:\n" + quote
        + "\n\nREVIEWER FINDING:\n" + (mark.get("heading") or "")
        + "\n" + (mark.get("detail") or "")
        + "\n\nHOW IT WAS REACHED:\n" + (mark.get("evidence") or "")
        + "\n\nRewrite the passage."
    )

    body = json.dumps({
        "model": name,
        "prompt": prompt,
        "system": SYSTEM,
        "stream": False,
        # Low temperature: this is contract prose, not copywriting.
        "options": {"temperature": 0.2, "num_predict": 700},
        "think": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        url + "/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise DraftUnavailable("local model returned HTTP " + str(e.code)) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DraftUnavailable(
            "no local model host on " + url + " (" + type(e).__name__ + ")") from e

    proposed = _clean(payload.get("response") or "")

    if not proposed or proposed.upper().startswith("CANNOT DRAFT"):
        raise DraftUnavailable(
            "the model declined to draft this one rather than invent a source for it. "
            "That is the correct outcome when the finding is an unsupported claim.",
            retryable=False,
        )

    low = proposed.lower()
    hit = [w for w in FORBIDDEN if w in low]
    if hit:
        # Not shown, not offered, not softened. The one sentence this system may
        # never put in front of a reviewer is the one a model reaches for first.
        raise DraftUnavailable(
            "the draft asserted a verdict (" + ", ".join(hit) + ") and was discarded. "
            "The tool may propose words; it may not propose a conclusion.",
            retryable=True,
        )

    made_up = fabrications(quote, proposed)
    if made_up:
        raise DraftUnavailable(
            "the draft introduced " + ", ".join(made_up) + ", which appears nowhere in "
            "the passage. A reference nobody wrote is the one thing this tool must never "
            "put in a document, so the draft was discarded rather than shown.",
            retryable=True,
        )

    warnings = []
    if len(proposed) > max(400, len(quote) * 3):
        warnings.append("The draft is much longer than the passage it replaces.")

    return Draft(
        mark_id=mark.get("mark_id", ""),
        original=quote,
        proposed=proposed,
        model=name,
        warnings=warnings,
    )
