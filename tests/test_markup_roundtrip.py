"""
Tests for §5.3's markup round-trip — as much of it as is reachable without
Word.

The real acceptance line for markup is:

    mark up -> remove -> the document's XML is byte-identical to before
    markup [...] This must hold across a close and reopen in Marked up mode.

That is a statement about actual Word documents, produced by actual
`insertContentControl` / `insertComment` / `customXmlParts` calls in
addin/taskpane.js and addin/anchor.js — there is no way to exercise it from
pytest, because there is no Word host here. What *is* reachable headlessly is
every piece of that round-trip that is pure logic rather than a Word API
call: the CGATE: tag format content controls are found and removed by, the
restore journal's XML encode/decode, and the palette/label guarantees §9.2-3
require. tests/js/taskpane_pure_logic.node.js exercises those directly
against the real addin/taskpane.js source (not a reimplementation of it) via
Node's `vm` module, the same way tests/test_anchor.py exercises cgate/anchor.py
directly.

This file is the pytest entry point for that harness. Skips (not fails) if
Node is not on PATH, since Node is a dev-time convenience here, not a cgate
runtime dependency (see requirements.txt's "deliberately small" rule).

**Manual verification still required** (§12.3): open the marked-up tender in
Word, mark it up in each of the three intensities, remove markup, and confirm
byte-for-byte round-trip — including across a close/reopen in Marked up mode,
which is what the restore journal exists for. Record what you saw.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "js", "taskpane_pure_logic.node.js")


def test_taskpane_pure_logic_harness_exists():
    assert os.path.exists(HARNESS)


def test_taskpane_pure_logic_passes_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH — this check is a dev-time convenience, not a runtime dependency")

    result = subprocess.run(
        [node, HARNESS], cwd=HERE, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "taskpane.js pure-logic checks failed:\n\n" + result.stdout + "\n" + result.stderr
        )
