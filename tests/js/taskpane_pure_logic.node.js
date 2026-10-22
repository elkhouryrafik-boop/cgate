/* ============================================================
   Pure-logic checks for addin/taskpane.js, run under plain Node.

   Everything in this file is Word-independent by construction: tag
   build/parse, the journal XML encode/decode round-trip, the comment body's
   hash-prefix format, and search-fragment shortening. It exists because
   pytest cannot load a browser-hosted Office.js script — this loads
   taskpane.js into a `vm` context with minimal DOM/Office/window stubs (just
   enough that its top-level `wire(); boot();` does not throw) and then calls
   its plain functions directly, the same way tests/test_anchor.py calls
   cgate/anchor.py's plan_one() directly.

   What this does NOT cover, and cannot cover without Word: whether
   insertContentControl/insertComment/customXmlParts actually round-trip
   byte-identically across a real close and reopen (PLAN §5.3's acceptance
   line). That needs a human running the §12.3 manual harness in Word.

   Invoked by tests/test_markup_roundtrip.py via subprocess; not a pytest
   file itself, since it has no Python in it.
   ============================================================ */

'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ADDIN = path.join(__dirname, '..', '..', 'addin');
const read = (f) => fs.readFileSync(path.join(ADDIN, f), 'utf8');

// The same order taskpane.html loads them in. taskpane.js's wire() calls
// CGATE_MARKS.init(), so the marks module has to exist by then — loading them
// out of order here would pass a test the browser would fail.
const SCRIPTS = ['anchor.js', 'marks.js', 'taskpane.js'];
const SRC = read('taskpane.js');

function fakeEl() {
  const el = {
    hidden: false, disabled: false, value: '', textContent: '', innerHTML: '',
    className: '', dataset: {}, style: {},
  };
  el.addEventListener = () => {};
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.appendChild = () => {};
  el.setAttribute = () => {};
  el.getAttribute = () => null;
  el.scrollIntoView = () => {};
  el.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  return el;
}

const sandbox = {
  console,
  document: {
    getElementById: () => fakeEl(),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => fakeEl(),
    addEventListener() {},
  },
  window: {
    __officeUnavailable: true,           // boot() takes the standalone path — no Office.onReady
    location: { origin: 'https://localhost:8765' },
    localStorage: { getItem: () => null, setItem() {} },
    confirm: () => true,
    prompt: () => '',
    alert() {},
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
  },
  navigator: { clipboard: { writeText: () => Promise.resolve() } },
  FormData: function FormData() { this.append = () => {}; },
  Blob: function Blob() {},
  fetch: () => Promise.reject(new Error('no network in the node harness')),
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '_') },
};
sandbox.window.CSS = sandbox.CSS;
sandbox.globalThis = sandbox;

process.on('unhandledRejection', () => {}); // health()'s fetch() rejects; expected, harmless here

const context = vm.createContext(sandbox);
SCRIPTS.forEach((f) => vm.runInContext(read(f), context, { filename: f }));

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log('ok   ' + name);
  } catch (e) {
    failures += 1;
    console.log('FAIL ' + name + ' — ' + e.message);
  }
}

/* -------------------------------------------------- tag build/parse (§5.3) */

check('parseTag reads a single-provision tag', () => {
  const p = context.parseTag('CGATE:req0017:high');
  assert.deepEqual(p, { reqIds: ['req0017'], band: 'high' });
});

check('parseTag reads a coalesced tag (§4.1.4)', () => {
  const p = context.parseTag('CGATE:req0017+req0018+req0019:open');
  assert.deepEqual(p, { reqIds: ['req0017', 'req0018', 'req0019'], band: 'open' });
});

check('parseTag rejects a tag that is not ours', () => {
  assert.strictEqual(context.parseTag('SomeOtherAddin:req0017'), null);
  assert.strictEqual(context.parseTag(''), null);
  assert.strictEqual(context.parseTag(undefined), null);
});

/* ---------------------------------------------- journal XML round-trip (§5.3) */

check('journal XML round-trips ordinary entries', () => {
  const entries = [
    { tag: 'CGATE:req0001:high', underline: 'None' },
    { tag: 'CGATE:req0002+req0003:open', underline: 'Single' },
  ];
  const xml = context.journalXml(entries);
  const back = context.parseJournalXml(xml);
  assert.deepEqual(back, entries);
});

check('journal XML survives an ampersand and a quote in a tag', () => {
  // Tags never actually contain these characters (they're CGATE:<req_id>s:<band>),
  // but the encoder must not silently corrupt the document if one ever did.
  const entries = [{ tag: 'CGATE:req"0001&x:high', underline: 'Wave' }];
  const xml = context.journalXml(entries);
  assert.ok(xml.indexOf('req&quot;0001&amp;x') !== -1);
  const back = context.parseJournalXml(xml);
  assert.deepEqual(back, entries);
});

check('journal XML round-trips zero entries', () => {
  assert.deepEqual(context.parseJournalXml(context.journalXml([])), []);
});

/* --------------------------------------------- comment body / provenance (§6.4) */

check('commentBody opens with the CGATE mark, hash prefix, req_id and band', () => {
  const p = {
    req_id: 'req0017', exposure_label: 'Read the clause itself', where: 'p.10, clause 1.2',
    binds: 'Obliges the firm to act.', against: 'because reasons', manageable: '',
    found: 'Matched on "shall".', checklist_items: [3, 8],
  };
  const body = context.commentBody(p, 'a'.repeat(64));
  const first = body.split('\n')[0];
  assert.strictEqual(first, 'CGATE aaaaaaaaaaaa · req0017 · READ THE CLAUSE ITSELF · p.10, clause 1.2');
  assert.ok(body.indexOf('BEARS ON CHECKLIST ITEM(S): 3, 8') !== -1);
  assert.ok(body.indexOf('Reply Confirm / Reject') !== -1);
});

check('commentBody omits the hash prefix when no doc id is given', () => {
  const p = { req_id: 'req0017', exposure_label: 'Routine', where: 'p.1', binds: 'x', against: 'y',
    manageable: '', found: 'z', checklist_items: [] };
  const first = context.commentBody(p, '').split('\n')[0];
  assert.strictEqual(first, 'CGATE req0017 · ROUTINE · p.1');
});

/* -------------------------------------------------- control title (§6.4) */

check('controlTitle reads as a reference on its own (§6.4 example shape)', () => {
  const byId = { req0017: { req_id: 'req0017', category: 'R4', exposure: 'high',
    exposure_label: 'Read the clause itself', actor: 'both' } };
  const t = context.controlTitle(['req0017'], byId);
  assert.strictEqual(t, '[R4 · req0017] Read the clause itself — binds both');
});

check('controlTitle on a coalesced anchor leads with the worst member', () => {
  const byId = {
    req0001: { req_id: 'req0001', category: 'R1', exposure: 'routine',
      exposure_label: 'Routine', actor: 'contractor' },
    req0002: { req_id: 'req0002', category: 'R4', exposure: 'high',
      exposure_label: 'Read the clause itself', actor: 'contractor' },
  };
  const t = context.controlTitle(['req0001', 'req0002'], byId);
  assert.ok(t.indexOf('req0002') === 1 || t.indexOf('R4') !== -1, 'leads with the worst member');
  assert.ok(t.indexOf('(+1 more)') !== -1);
});

/* -------------------------------------------------- search fragments (§4.2 fallback) */

check('searchFragments never exceeds Word\'s 255-char search limit', () => {
  const long = Array.from({ length: 60 }, (_, i) => 'word' + i).join(' ');
  context.searchFragments(long).forEach((f) => assert.ok(f.length <= 250));
});

check('searchFragments strips the caret (Word\'s find escape)', () => {
  context.searchFragments('clause 6.2^ of the General Conditions').forEach((f) => {
    assert.ok(f.indexOf('^') === -1);
  });
});

/* -------------------------------------------------- band palette (§0.2 / §3.2) */

/** WCAG relative-luminance contrast against white. A boundary that a reader
 *  cannot see is not a boundary, whatever colour the code says it is. */
function contrastOnWhite(hex) {
  const chan = (v) => {
    const c = parseInt(v, 16) / 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const L = 0.2126 * chan(hex.slice(1, 3))
          + 0.7152 * chan(hex.slice(3, 5))
          + 0.0722 * chan(hex.slice(5, 7));
  return 1.05 / (L + 0.05);
}

check('no band colour is green, and the palette matches taskpane.css tokens', () => {
  assert.deepEqual(context.BAND_HEX, {
    high: '#b3261e', unclassified: '#0b2f90', open: '#8a5a00', routine: '#4a5578',
  });
  Object.values(context.BAND_HEX).forEach((hex) => {
    const g = parseInt(hex.slice(3, 5), 16);
    const r = parseInt(hex.slice(1, 3), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    assert.ok(!(g > r && g > b), hex + ' reads as green-dominant');
  });
  // Not merely "is this the agreed hex" — the reason it has to be this one.
  // A reviewed frame is drained on purpose, but a drain that reaches the
  // background has erased the distinction between read and never opened.
  assert.strictEqual(context.REVIEWED_HEX, '#8794b8');
  assert.ok(contrastOnWhite(context.REVIEWED_HEX) >= 3,
    'the reviewed state is ' + contrastOnWhite(context.REVIEWED_HEX).toFixed(2)
    + ':1 on white; below 3:1 it is indistinguishable from an unmarked paragraph');
});

check('every band has all three channels: colour, underline, English label', () => {
  ['high', 'unclassified', 'open', 'routine'].forEach((band) => {
    assert.ok(context.BAND_HEX[band]);
    assert.ok(context.BAND_UNDERLINE[band]);
    assert.ok(context.BAND_LABEL[band]);
  });
});

/* -------------------------------------------------- filters (§6.3) */

check('matchesFilters is the identity when no filter is set', () => {
  context.state.filters = { bands: new Set(), category: '', item: '', reviewed: '' };
  context.state.reviewedState = {};
  const p = { req_id: 'req0001', exposure: 'high', category: 'R4', checklist_items: [3] };
  assert.strictEqual(context.matchesFilters(p), true);
});

check('matchesFilters narrows by band, category, item and reviewed state', () => {
  context.state.filters = { bands: new Set(['open']), category: '', item: '', reviewed: '' };
  context.state.reviewedState = {};
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'high', category: 'R1', checklist_items: [] }), false);
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'open', category: 'R1', checklist_items: [] }), true);

  context.state.filters = { bands: new Set(), category: 'R4', item: '', reviewed: '' };
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'open', category: 'R1', checklist_items: [] }), false);
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'open', category: 'R4', checklist_items: [] }), true);

  context.state.filters = { bands: new Set(), category: '', item: '12', reviewed: '' };
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'open', category: 'R4', checklist_items: [11] }), false);
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'open', category: 'R4', checklist_items: [12] }), true);

  context.state.filters = { bands: new Set(), category: '', item: '', reviewed: 'unreviewed' };
  context.state.reviewedState = { a: { kind: 'confirm', reviewer: 'X' } };
  assert.strictEqual(context.matchesFilters({ req_id: 'a', exposure: 'open', category: 'R4', checklist_items: [] }), false);
  assert.strictEqual(context.matchesFilters({ req_id: 'b', exposure: 'open', category: 'R4', checklist_items: [] }), true);
});

/* -------------------------------------------------- rehydration reshape (§6.1) */

check('reviewJsonToPayload carries provisions through and marks itself rehydrated', () => {
  const stored = {
    doc_id: 'x'.repeat(64), doc_path: 'C:\\tmp\\tender.docx', generated_at: 't',
    tool_version: '1.0', checklist_version: '1', stats: { pages: 15, lexicon_version: '3' },
    provisions: [{ req_id: 'req0001', exposure: 'high' }],
    ingest_notes: [], errors: [],
  };
  const payload = context.reviewJsonToPayload(stored);
  assert.strictEqual(payload.doc_name, 'tender.docx');
  assert.strictEqual(payload.rehydrated, true);
  assert.strictEqual(payload.provisions.length, 1);
  assert.strictEqual(payload.items.length, 0); // Checklist/Gaps need a fresh review — by design
  assert.strictEqual(payload.lexicon_version, '3');
});

/* -------------------------------------------------- bytes/base64 */

check('bytesToBase64 matches Buffer for arbitrary bytes', () => {
  const bytes = new Uint8Array([0, 1, 2, 253, 254, 255, 65, 66, 67]);
  const got = context.bytesToBase64(bytes);
  const want = Buffer.from(bytes).toString('base64');
  assert.strictEqual(got, want);
});

/* ------------------------------------------------------------------------ */

if (failures) {
  console.error('\n' + failures + ' check(s) failed');
  process.exit(1);
}
/* ---------------------------------------------------------------- marks --
   The examiner's marks. Word-independent parts only: the tag round-trip, the
   comment body, and the two design rules that must not drift.
*/

const MARKS = vm.runInContext('CGATE_MARKS', context);

check('marks: tag round-trips', () => {
  const M = MARKS;
  const tag = M.TAG + 'm1+m2:correction';
  const parsed = M.parseTag(tag);
  // Compared by value, not with deepStrictEqual: an array built inside the vm
  // context has that realm's Array prototype, so a structural comparison fails
  // on identity alone and says nothing about the parse.
  assert.strictEqual(parsed.markIds.join('+'), 'm1+m2');
  assert.strictEqual(parsed.kind, 'correction');
});

check('marks: a foreign tag is not ours', () => {
  const M = MARKS;
  assert.strictEqual(M.parseTag('CGATE:r1:high'), null);
  assert.strictEqual(M.parseTag(''), null);
  assert.strictEqual(M.parseTag(null), null);
});

check('marks: a commendation adds no underline', () => {
  // Underlining every sound paragraph would deface the document to say
  // "this is fine". The margin already says it.
  const M = MARKS;
  assert.strictEqual(M.UNDERLINE.commendation, 'None');
  assert.strictEqual(M.UNDERLINE.correction, 'Wave');
  assert.strictEqual(M.UNDERLINE.query, 'Dotted');
});

check('marks: every kind is distinguishable without colour', () => {
  // A compliance surface must not let a reader skim status by colour alone.
  const M = MARKS;
  const glyphs = M.KINDS.map((k) => M.GLYPH[k]);
  assert.strictEqual(new Set(glyphs).size, M.KINDS.length);
  const hexes = M.KINDS.map((k) => M.HEX[k]);
  assert.strictEqual(new Set(hexes).size, M.KINDS.length);
});

check('marks: no mark colour is green either', () => {
  // The band palette has been checked for green since §0.2 was written. The
  // *mark* palette was not, and drifted: the commendation shipped as #0f6b3f,
  // a green, on the one mark where green is most dangerous. A `✓` drawn in
  // green reads as "cleared" to every reviewer alive, and nothing in this
  // system is cleared. Both palettes are guarded from here.
  const M = MARKS;
  Object.entries(M.HEX).forEach(([kind, hex]) => {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    assert.ok(!(g > r && g > b), kind + ' is ' + hex + ', which reads as green-dominant');
  });
});

check('marks: the comment carries its reasoning and disclaims determination', () => {
  const M = MARKS;
  const body = M.commentBody({
    kind: 'correction',
    heading: 'Riley Stone not verified',
    detail: 'Not on the staff register.',
    evidence: 'Compared against the staff register supplied to this run.',
    checklist_items: [13, 14],
  }, 'a'.repeat(64));

  assert.ok(body.indexOf('CGATE ✗ CORRECTION') === 0, 'starts with the mark');
  assert.ok(body.includes('aaaaaaaaaaaa'), 'carries the document hash prefix');
  assert.ok(body.includes('HOW THIS WAS REACHED:'), 'carries its reasoning');
  assert.ok(body.includes('13, 14'), 'names the checklist items');
  assert.ok(body.includes('Not a determination of the Compliance Officer'),
            'disclaims determination');
});

check('marks: no comment ever says cleared, compliant or satisfied', () => {
  const M = MARKS;
  const body = M.commentBody({
    kind: 'commendation',
    heading: 'Alex Rowan verified',
    detail: 'On the staff register as Team Leader.',
    evidence: 'Matched against the staff register supplied to this run.',
    checklist_items: [13],
  }, 'b'.repeat(64)).toLowerCase();
  ['cleared', 'compliant', 'satisfied', 'passed', 'approved'].forEach((w) => {
    assert.ok(!body.includes(w), 'comment must not say ' + w);
  });
});

// `failures` was counted and never read: every FAIL printed, and the process
// still exited 0, so pytest saw green unless the script crashed outright.
if (failures) {
  console.error('\n' + failures + ' pure-logic check(s) FAILED');
  process.exit(1);
}
console.log('\nall pure-logic checks passed');
