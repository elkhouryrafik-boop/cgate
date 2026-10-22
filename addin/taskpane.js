/* ============================================================
   Compliance Review — task pane
   ============================================================

   Two host situations, one pane:

     In Word    the document is already open, so the pane asks Word for its
                bytes, reviews them, and can then put what it found back in the
                margin next to the clause it came from.

     In a browser  there is no open document, so files are dropped in. This is
                how a PDF solicitation is reviewed, since Word would have to
                convert it first and a converted copy is not the file the firm
                is bidding against.

   The rule the pane holds everywhere: nothing it renders may read as a
   clearance. There is no green, no tick, and no state that means clean. What it
   reports is exposure — what a clause binds the firm to, and whether anything
   on file shows the firm can meet it.

   This file implements the in-document review surface:
   the document itself becomes the review surface (frames, comments, the
   document going grey as work proceeds, walk mode, filters that reach into
   the document, the minimap, and the margin as a decision surface via comment
   replies). The hard logic (the anchor plan) lives in anchor.js, loaded
   before this file — this file only executes it and renders around it.
*/

'use strict';

var API = window.location.origin;
var COMMENT_MARK = 'CGATE';          // how the pane recognises its own markup
var SEARCH_MAX = 250;                // Word refuses a search string over 255

// PLAN §3.2 — verbatim from addin/taskpane.css :root tokens. Kept as literal
// hex here (not read from CSS) because these are set on Word ContentControl
// objects, which have no notion of a CSS custom property. If the palette ever
// changes, change it in both places — see the "No green" rule (§0.2) before
// touching either.
var BAND_HEX = {
  high: '#b3261e',
  unclassified: '#0b2f90',
  open: '#8a5a00',
  routine: '#4a5578',
};
var BAND_UNDERLINE = { high: 'Wave', unclassified: 'DashLine', open: 'Dotted', routine: 'None' };
var BAND_LABEL = {
  high: 'Read the clause itself',
  unclassified: 'Not classified',
  open: 'Binds us, unproven',
  routine: 'Routine',
};
// A state, not a band (§3.4). It was --royal-100 (#dbe2f2), which is 1.30:1
// against white — below the 3:1 floor for a non-text boundary, so a reviewed
// frame and an unmarked paragraph looked the same on screen and printed the
// same in greyscale. That inverts the rule this whole system runs on: a clause
// somebody read and a clause nobody opened must never look alike. Drained, but
// still legible, at 3.02:1.
var REVIEWED_HEX = '#8794b8';

var ACTOR_SHORT = {
  contractor: 'binds us',
  subcontractor: 'binds our counterparties',
  personnel: 'binds our people',
  un: 'binds the client',
  both: 'binds both',
  unknown: 'actor unresolved',
};

var state = {
  inWord: false,
  canComment: false,       // WordApi 1.4 — comments, changeTrackingMode, customXmlParts
  canExpand: false,        // WordApi 1.3 — Range.expandTo, two-point anchors
  canCustomXml: false,     // WordApi 1.4 — restore journal (§5.3)
  canStampProvenance: false, // WordApi 1.3 — document.properties.customProperties
  review: null,
  bundleFile: null,
  files: [],

  anchorExec: {},          // req_id -> {result, reason, anchorId, band, coalescedWith}
  reviewedState: {},       // req_id -> {kind, reviewer, category, ts}
  ingestedReplyKeys: new Set(),

  markupMode: 'frames',    // 'frames' | 'marked-up' | 'comments'
  markupTarget: 'copy',    // 'copy' | 'this'
  showReviewed: false,

  walkOrder: [],
  walkIndex: -1,

  filters: { bands: new Set(), category: '', item: '', reviewed: '' },
};

const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- status */

function say(msg, isError) {
  const el = $('status');
  el.innerHTML = msg;
  el.classList.toggle('err', !!isError);
}

function busy(msg) {
  say('<span class="spinner"></span>' + msg);
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ------------------------------------------------------------------ boot */

/* ------------------------------------------------- the examiner's marks ---

   The other direction of review: this document is ours, and wants marking.
   `marks.js` owns the rendering and the Word writes; everything here is the
   glue that hands it the pane and the optional sources.
*/

function marksSources() {
  const material = Array.from(($('mk-material').files) || []);
  const register = ($('mk-register').files || [])[0] || null;
  const partiesRaw = ($('mk-parties').value || '').trim();
  const parties = partiesRaw
    ? partiesRaw.split(',').map((x) => x.trim()).filter(Boolean)
    : null;
  const solicitationDocId = ($('mk-solicitation').value || '').trim() || null;
  return { material, register, parties, solicitationDocId };
}

function renderMarks() {
  const payload = CGATE_MARKS.current();
  if (!payload) return;
  $('mk-name').textContent = (payload.submission_path || '').split(/[\\/]/).pop();
  $('mk-hash').textContent = payload.submission_id;
  $('mk-hash').onclick = () => {
    navigator.clipboard.writeText(payload.submission_id)
      .then(() => say('Document hash copied.'))
      .catch(() => say('Could not copy the hash.', true));
  };
  const n = ['register', 'material', 'parties', 'solicitation']
    .filter((k) => {
      const el = $('mk-' + k);
      return el && (el.files ? el.files.length : (el.value || '').trim());
    }).length;
  $('mk-src-n').textContent = n ? n + ' supplied' : 'none supplied';
  CGATE_MARKS.render($('mk-list'));
}

async function reviewSubmission() {
  if (!state.inWord) {
    say('Marking up our own submission needs the document open in Word.', true);
    return;
  }
  busy('Reading the submission …');
  try {
    await CGATE_MARKS.runReview(marksSources());
    $('idle').hidden = true;
    $('result').hidden = true;
    $('marks').hidden = false;
    renderMarks();
    const c = CGATE_MARKS.current().counts || {};
    say(c.correction + ' correction(s), ' + c.query + ' query/queries, ' +
        c.commendation + ' verified.');
  } catch (e) {
    say('Could not review the submission: ' + e.message, true);
  }
}

async function markUpSubmission(mode) {
  try {
    const out = await CGATE_MARKS.markUp(mode);
    let msg = out.placed + ' of ' + out.anchorable + ' mark(s) placed in the document.';
    if (out.unanchored > 0) {
      // Absent is not clean: an unplaced mark is still in the pane, and the
      // reviewer is told the count rather than left to notice the gap.
      msg += ' ' + out.unanchored + ' could not be anchored and remain in this pane only.';
    }
    say(msg);
  } catch (e) {
    say('Could not mark up: ' + e.message, true);
  }
}

async function removeSubmissionMarks() {
  try {
    const n = await CGATE_MARKS.removeMarkup();
    say(n ? n + ' mark(s) removed.' : 'No examiner marks were present.');
  } catch (e) {
    say('Could not remove marks: ' + e.message, true);
  }
}

function boot() {
  if (window.__officeUnavailable || typeof Office === 'undefined') {
    startStandalone('Office.js could not be loaded. Running standalone — drop files to review.');
    return;
  }
  // Office.onReady can hang forever on the first pane load of a Word session
  // (a WebView2 cold-start race in the office.js host handshake; a reload of
  // the same page completes it). One automatic reload, marked in
  // sessionStorage so a genuinely broken handshake cannot loop.
  setTimeout(() => {
    if (window.__officeReady) return;
    let again = false;
    try {
      again = !sessionStorage.getItem('cgate-onready-reload');
      if (again) sessionStorage.setItem('cgate-onready-reload', '1');
    } catch (e) { /* storage unavailable: do not risk a reload loop */ }
    if (again) location.reload();
  }, 6000);
  Office.onReady((info) => {
    window.__officeReady = true;
    state.inWord = info && info.host === Office.HostType.Word;
    if (state.inWord) {
      const supports = (v) => {
        try { return Office.context.requirements.isSetSupported('WordApi', v); }
        catch (e) { return false; }
      };
      state.canComment = supports('1.4');
      state.canCustomXml = supports('1.4');
      state.canExpand = supports('1.3');
      state.canStampProvenance = supports('1.3');

      $('host-actions').hidden = false;
      $('standalone-actions').hidden = true;
      $('btn-sync-replies').hidden = !state.canComment;
      health();
      attemptRehydration();
    } else {
      startStandalone('Not running in Word — drop files to review.');
    }
  });
}

function startStandalone(why) {
  state.inWord = false;
  $('host-actions').hidden = true;
  $('standalone-actions').hidden = false;
  $('doc-tools').hidden = true;
  health(why);
}

async function health(prefix) {
  try {
    const r = await fetch(API + '/api/health');
    const j = await r.json();
    say((prefix ? prefix + ' ' : '') + 'Review host up · cgate ' + escapeHtml(j.tool_version) +
        ' · documents stay on this machine.');
  } catch (e) {
    say('No review host on ' + escapeHtml(API) +
        '. Start it with <b>cgate serve</b> in the tool directory.', true);
  }
}

/* -------------------------------------------------------- get the bytes */

/** Ask Word for the open document as .docx bytes, in slices. */
function documentBytes() {
  return new Promise((resolve, reject) => {
    Office.context.document.getFileAsync(
      Office.FileType.Compressed,
      { sliceSize: 65536 },
      (res) => {
        if (res.status !== Office.AsyncResultStatus.Succeeded) {
          // Word's own message here is "An internal error has occurred", which
          // tells the reader nothing. By far its commonest cause is a document
          // that has never been written to disk — which is exactly what you get
          // after Word converts a PDF, the way this tool is most often opened.
          // Name that case rather than relaying the string.
          let saved = false;
          try {
            const url = Office.context.document.url || '';
            // A document Word converted from a PDF still reports the .pdf path
            // as its url, but has never been written to disk as .docx — Word
            // will not hand it over until it is.
            saved = !!url && !/\.pdf$/i.test(url);
          } catch (e) { /* not available */ }
          reject(new Error(
            saved
              ? (res.error ? res.error.message : 'Word would not hand over the file')
              : 'this document has not been saved yet, and Word will not hand over an '
                + 'unsaved file. Save it as .docx (Ctrl+S) and review it again. '
                + 'A converted PDF is unsaved until you do.'
          ));
          return;
        }
        const file = res.value;
        const chunks = new Array(file.sliceCount);
        let got = 0;
        let failed = false;

        for (let i = 0; i < file.sliceCount; i++) {
          file.getSliceAsync(i, (sr) => {
            if (failed) return;
            if (sr.status !== Office.AsyncResultStatus.Succeeded) {
              failed = true;
              file.closeAsync(() => {});
              reject(new Error('slice ' + i + ' failed'));
              return;
            }
            chunks[sr.value.index] = sr.value.data;
            busy('Reading the document from Word … ' + (++got) + '/' + file.sliceCount);
            if (got === file.sliceCount) {
              file.closeAsync(() => {});
              let total = 0;
              chunks.forEach((c) => { total += c.length; });
              const bytes = new Uint8Array(total);
              let at = 0;
              chunks.forEach((c) => { bytes.set(c, at); at += c.length; });
              resolve(bytes);
            }
          });
        }
      }
    );
  });
}

function documentName() {
  try {
    const url = Office.context.document.url || '';
    const base = url.split(/[\\/]/).pop();
    if (!base) return 'document.docx';
    return /\.docx$/i.test(base) ? base : base.replace(/\.[^.]*$/, '') + '.docx';
  } catch (e) {
    return 'document.docx';
  }
}

/** Uint8Array -> base64, in chunks so String.fromCharCode does not blow the
 *  call stack on a large solicitation. */
function bytesToBase64(bytes) {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return window.btoa(binary);
}

/* ---------------------------------------------------------------- review */

async function post(form) {
  const r = await fetch(API + '/api/review', { method: 'POST', body: form });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep statusText */ }
    throw new Error(detail);
  }
  return r.json();
}

async function reviewFromWord() {
  $('btn-review-doc').disabled = true;
  try {
    busy('Asking Word for the document …');
    const bytes = await documentBytes();
    const form = new FormData();
    form.append('files', new Blob([bytes]), documentName());
    form.append('name', documentName());
    if (state.bundleFile) form.append('bundle', state.bundleFile);
    busy('Segmenting clauses and extracting provisions …');
    render(await post(form));
  } catch (e) {
    say('Review failed: ' + escapeHtml(e.message), true);
  } finally {
    $('btn-review-doc').disabled = false;
  }
}

async function reviewFromFiles() {
  if (!state.files.length) return;
  $('btn-review-files').disabled = true;
  try {
    const form = new FormData();
    state.files.forEach((f) => form.append('files', f, f.name));
    form.append('name', state.files[0].name);
    if (state.bundleFile) form.append('bundle', state.bundleFile);
    busy('Reviewing ' + state.files.length + ' file(s) …');
    render(await post(form));
  } catch (e) {
    say('Review failed: ' + escapeHtml(e.message), true);
  } finally {
    $('btn-review-files').disabled = false;
  }
}

/** Rebuild the JSON /api/review would have returned from what was written to
 *  disk (review.json), for §6.1 rehydration. Deliberately narrower than a
 *  live review: `items` is left empty (the Checklist/Gaps tabs need a fresh
 *  "Review this document" to repopulate) — what rehydration exists for is the
 *  provisions surface, the reviewed states, and the minimap, which review.json
 *  carries in full. */
function reviewJsonToPayload(stored) {
  return {
    doc_id: stored.doc_id,
    doc_path: stored.doc_path,
    doc_name: (stored.doc_path || '').split(/[\\/]/).pop() || 'document.docx',
    generated_at: stored.generated_at,
    tool_version: stored.tool_version,
    checklist_version: stored.checklist_version,
    lexicon_version: (stored.stats || {}).lexicon_version || '0',
    stats: stored.stats || {},
    counts: {},
    blocking: [],
    items: [],
    provisions: stored.provisions || [],
    ingest_notes: stored.ingest_notes || [],
    errors: stored.errors || [],
    calibration: {},
    artifacts: {},
    rehydrated: true,
  };
}

/** §6.1 — a document already carrying CGATE controls resumes where it was
 *  left, with no session state on the host: the tags/titles/properties are
 *  the save file. Best-effort and silent on failure; it is a convenience on
 *  top of "Review this document", never a requirement for using the pane. */
async function attemptRehydration() {
  let docId = null;
  try {
    await Word.run(async (context) => {
      const ccs = context.document.contentControls;
      ccs.load('items/tag');
      const props = context.document.properties.customProperties;
      props.load('items/key/value');
      await context.sync();

      const ours = ccs.items.filter((c) => (c.tag || '').indexOf('CGATE:') === 0);
      if (!ours.length) return;
      const hashProp = props.items.find((p) => p.key === 'CGATE_DocHash');
      if (hashProp) docId = hashProp.value;
    });
  } catch (e) {
    return; // not fatal — the reviewer can still press "Review this document"
  }
  if (!docId) return;

  try {
    busy('Reopening a previous review of this document …');
    const r = await fetch(API + '/api/review/' + docId + '/json');
    if (!r.ok) {
      say('This document carries CGATE markup from a previous review, but that review is no ' +
          'longer on this machine. Review it again to relink.', true);
      return;
    }
    const stored = await r.json();
    render(reviewJsonToPayload(stored));

    const dr = await fetch(API + '/api/review/' + docId + '/decisions');
    if (dr.ok) {
      const dj = await dr.json();
      (dj.decisions || []).forEach((row) => {
        state.reviewedState[row.req_id] = {
          kind: row.kind, reviewer: row.reviewer, category: row.category, ts: row.ts,
        };
      });
      renderProvisions(state.review);
    }
    say('Reopened a previous review of this document from its own markup. ' +
        Object.keys(state.reviewedState).length + ' decision(s) restored from the ledger. ' +
        'Checklist and Gaps need “Review this document” again for full detail.');
  } catch (e) {
    say('Could not reopen the previous review: ' + escapeHtml(e.message), true);
  }
}

/* ---------------------------------------------------------------- render */

function render(payload) {
  state.review = payload;
  state.reviewedState = {};
  state.anchorExec = {};
  state.ingestedReplyKeys = new Set();
  state.walkIndex = -1;
  state.filters = { bands: new Set(), category: '', item: '', reviewed: '' };

  $('idle').hidden = true;
  $('result').hidden = false;
  $('doc-tools').hidden = !state.inWord;
  $('unanchored-wrap').hidden = true;
  $('minimap').hidden = true;

  const s = payload.stats;
  $('r-name').textContent = payload.doc_name;
  $('r-stats').textContent =
    (s.pages || 0) + 'pp · ' + (s.clauses || 0) + ' clauses · ' + (s.requirements_kept || 0) +
    ' provisions kept · ' + (s.requirements_dropped || 0) + ' dropped by the grounding gate' +
    (s.elapsed_seconds != null ? ' · ' + s.elapsed_seconds + 's' : '');
  $('r-hash').textContent = 'sha256 ' + payload.doc_id.slice(0, 32) + '…';
  $('r-hash').onclick = () => {
    navigator.clipboard.writeText(payload.doc_id)
      .then(() => say('Document hash copied.'))
      .catch(() => say('Could not copy — the hash is in review.json.', true));
  };

  $('n-prov').textContent = payload.provisions.length;
  const gaps = payload.items.filter((i) => i.status === 'unresolved');
  $('n-gaps').textContent = gaps.length;

  $('dl-request').href = API + '/api/review/' + payload.doc_id + '/request';
  $('dl-md').href = API + '/api/review/' + payload.doc_id + '/md';
  $('dl-json').href = API + '/api/review/' + payload.doc_id + '/json';

  renderProvisions(payload);
  renderChecklist(payload);
  renderGaps(payload, gaps);
  renderFilterBar(payload);

  state.walkOrder = computeWalkOrder(payload);
  updateWalkBar();
  $('walk-bar').hidden = !(payload.provisions.length);
  $('filter-bar').hidden = !(payload.provisions.length);

  const c = payload.counts || {};
  const bits = [];
  if (c.flag) bits.push(c.flag + ' flagged');
  if (c.requires_human) bits.push(c.requires_human + ' for CO determination');
  if (c.evidence_present) bits.push(c.evidence_present + ' with evidence on file');
  if (c.unresolved) bits.push(c.unresolved + ' unresolved');
  if (!payload.rehydrated) say('Reviewed. ' + bits.join(' · ') + '.');

  if (payload.blocking && payload.blocking.length) {
    say('BLOCKING: ' + payload.blocking.map((e) => escapeHtml(e.message)).join('; '), true);
  }
}

var MARKS = {
  high: 'mark-high',
  unclassified: 'mark-unclassified',
  open: 'mark-open',
  routine: 'mark-routine',
};

// Triage order. Worst first, then what the parser could not read, then what
// binds us with nothing on file, then the rest.
var EXPOSURE_ORDER = { high: 0, unclassified: 1, open: 2, routine: 3 };

function provisionsById() {
  const map = {};
  (state.review && state.review.provisions || []).forEach((p) => { map[p.req_id] = p; });
  return map;
}

function reviewedLabel(r) {
  if (!r) return 'reviewed';
  if (r.kind === 'confirm') return 'confirmed by ' + r.reviewer;
  if (r.kind === 'reject') return 'rejected by ' + r.reviewer;
  if (r.kind === 'classify') return 'classified ' + r.category + ' by ' + r.reviewer;
  return 'reviewed by ' + r.reviewer;
}

function paintCardReviewed(card, r) {
  if (!card) return;
  card.classList.toggle('reviewed', !state.showReviewed);
  const dot = card.querySelector('.decided');
  if (dot) dot.textContent = reviewedLabel(r);
  ['.act-confirm', '.act-reject', '.act-classify'].forEach((sel) => {
    const b = card.querySelector(sel);
    if (b) b.disabled = true;
  });
}

function provisionCard(p) {
  const el = document.createElement('article');
  el.className = 'prov';
  el.dataset.exposure = p.exposure;   // drives the card rule; colour stays on the mark
  el.dataset.reqId = p.req_id;

  const items = p.checklist_items && p.checklist_items.length
    ? '<div class="field"><dt>Bears on checklist item</dt><dd>' +
      p.checklist_items.map((i) => '#' + i).join(', ') +
      ' of SOP-3.11</dd></div>'
    : '';

  const carves = (p.exceptions || []).map((ex) => (
    '<div class="carve' + (ex.scope_resolved ? ' resolved' : '') + '">' +
      '<b>Carve-out</b> <span class="code">' + escapeHtml(ex.marker) + '</span> — ' +
      (ex.scope_resolved ? 'scope resolved' : '<b>scope unresolved</b>') + '.<br>' +
      escapeHtml(ex.text) +
      (ex.scope_note ? '<br><i>' + escapeHtml(ex.scope_note) + '</i>' : '') +
    '</div>'
  )).join('');

  const inc = p.incorporation
    ? '<div class="carve' + (p.incorporation.retrieved ? ' resolved' : '') + '">' +
      '<b>Incorporates by reference:</b> ' + escapeHtml(p.incorporation.target || '') + ' — ' +
      (p.incorporation.retrieved ? 'in the bundle.' : '<b>not attached.</b> Its content is unknown to this review and was not assumed.') +
      '</div>'
    : '';

  const trig = (p.triggers || []).length
    ? '<div style="margin-top:6px">Triggers: ' +
      p.triggers.map((t) => '<span class="code">' + escapeHtml(t) + '</span>').join(' ') +
      '</div>'
    : '';

  const anchor = state.anchorExec[p.req_id];
  const anchorNote = anchor
    ? '<div class="anchor-note"><b>Anchor: ' + escapeHtml(anchor.result) + '.</b>' +
      (anchor.coalescedWith && anchor.coalescedWith.length
        ? ' Shares one frame with ' + anchor.coalescedWith.map(escapeHtml).join(', ') + '.'
        : '') +
      (anchor.reason ? ' ' + escapeHtml(anchor.reason) : '') +
      '</div>'
    : '';

  el.innerHTML =
    '<div class="prov-head">' +
      '<span class="prov-mark exp-' + p.exposure + '">' +
        '<span class="mark ' + MARKS[p.exposure] + '"></span>' +
        '<span class="prov-label">' + escapeHtml(p.exposure_label) + '</span>' +
      '</span>' +
      '<span class="prov-where">' + escapeHtml(p.req_id) + ' · ' + escapeHtml(p.where) + '</span>' +
    '</div>' +

    '<blockquote class="quote">' + escapeHtml(p.quote) + '</blockquote>' +

    '<div class="field"><dt>Binds</dt><dd>' + escapeHtml(p.binds) + '</dd></div>' +
    '<div class="field field-against"><dt>Reads against us</dt><dd>' + escapeHtml(p.against) + '</dd></div>' +
    (p.manageable
      ? '<div class="field field-manageable"><dt>Manageable because</dt><dd>' + escapeHtml(p.manageable) + '</dd></div>'
      : '') +
    items +

    // The summary used to read "How the system got here", which is a label
    // announcing that an explanation exists — a lecture, and one the reviewer
    // has to open before it tells them anything. It carries the evidence
    // itself now: what was matched, under which category, and that a rule did
    // it rather than a model. Opening it still yields the full account, so
    // PLAN §0.4 holds: the reasoning is one interaction away and never absent.
    '<details class="reasoning"><summary>' +
      (p.matched && p.matched.length
        ? 'matched <span class="code">' + escapeHtml(p.matched.slice(0, 2).join('”, “')) + '</span> · '
        : '') +
      '<span class="code">' + escapeHtml(p.category) + '</span> · rule, not a model' +
    '</summary>' +
      '<div class="reasoning-body">' + escapeHtml(p.found) + carves + inc + trig + anchorNote +
      '<div style="margin-top:6px">Disagree? Rejecting this records the reason against ' +
      '<span class="code">' + escapeHtml(p.category) + '</span> in the calibration ledger. ' +
      'Nothing changes the extractor until a named person approves it with ' +
      '<span class="code">cgate learn --approve</span>.</div>' +
      '</div>' +
    '</details>' +

    '<div class="prov-acts">' +
      (state.inWord ? '<button class="btn btn-secondary btn-sm act-goto">Go to clause</button>' : '') +
      '<button class="btn btn-secondary btn-sm act-confirm">Confirm</button>' +
      '<button class="btn btn-secondary btn-sm act-reject">Reject</button>' +
      (p.exposure === 'unclassified'
        ? '<button class="btn btn-secondary btn-sm act-classify">Classify</button>' : '') +
      '<span class="decided"></span>' +
    '</div>';

  // Ninety-five cards at full height is seventeen thousand pixels of scroll, and
  // the fold showed one and a half of them. Collapsed, a card is its band, its
  // reference and two lines of the clause — enough to decide whether to open it
  // — and four fit above the fold. The head is the control; the whole card is
  // never a click target, because Go to clause and Confirm live inside it.
  const head = el.querySelector('.prov-head');
  head.setAttribute('role', 'button');
  head.setAttribute('tabindex', '0');
  head.setAttribute('aria-expanded', 'false');
  const toggle = () => {
    const open = el.classList.toggle('is-open');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  head.onclick = toggle;
  head.onkeydown = (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); }
  };

  const goto = el.querySelector('.act-goto');
  if (goto) goto.onclick = () => goToClause(p);
  el.querySelector('.act-confirm').onclick = () => decide(p, 'confirm', el);
  el.querySelector('.act-reject').onclick = () => decide(p, 'reject', el);
  const classifyBtn = el.querySelector('.act-classify');
  if (classifyBtn) classifyBtn.onclick = () => classify(p, el);

  if (state.reviewedState[p.req_id]) paintCardReviewed(el, state.reviewedState[p.req_id]);
  if (!matchesFilters(p)) el.hidden = true;

  return el;
}

function renderProvisions(payload) {
  const host = $('provisions');
  host.innerHTML = '';
  if (!payload.provisions.length) {
    host.innerHTML = '<div class="empty">No integrity, eligibility or conflict-of-interest ' +
      'provision was extracted from this document. That is a finding about the document, ' +
      'not a clean result for the firm.</div>';
    $('minimap').hidden = true;
    $('unanchored-wrap').hidden = true;
    return;
  }
  // Worst first: a reviewer who reads only the top of the pane must see the
  // sharpest thing in the document, not the first thing in it.
  payload.provisions
    .slice()
    .sort((a, b) =>
      (EXPOSURE_ORDER[a.exposure] - EXPOSURE_ORDER[b.exposure]) || (a.page - b.page))
    .forEach((p) => host.appendChild(provisionCard(p)));

  renderMinimap(payload);
  renderUnanchored(payload);
}

function renderChecklist(payload) {
  const host = $('checklist');
  host.innerHTML = '';
  let section = null;
  payload.items.forEach((it) => {
    if (it.section !== section) {
      section = it.section;
      const h = document.createElement('div');
      h.className = 'sec-head';
      h.textContent = 'Section ' + section;
      host.appendChild(h);
    }
    const d = document.createElement('details');
    d.className = 'item';
    const notes = (it.notes || []).map((n) => '<li>' + escapeHtml(n) + '</li>').join('');
    const trig = (it.triggers || []).length
      ? '<div style="margin-top:5px">Triggers fired: ' +
        it.triggers.map((t) => '<span class="code" style="font-family:var(--font-mono)">' +
          escapeHtml(t) + '</span>').join(' ') + '</div>'
      : '';
    const never = it.never_auto_clear
      ? '<div class="never">Never cleared automatically. Complete evidence moves this to the ' +
        'Compliance Officer for determination — it does not move it to clean.</div>'
      : '';
    d.innerHTML =
      '<summary>' +
        '<span class="id">' + it.item_id + '</span>' +
        '<span class="txt">' + escapeHtml(it.text) + '</span>' +
        '<span class="st st-' + it.status + '">' + escapeHtml(it.status_label) + '</span>' +
      '</summary>' +
      '<div class="item-body">' +
        '<div style="margin-bottom:4px">Handling: <b>' + escapeHtml(it.handling) + '</b></div>' +
        (notes ? '<ul>' + notes + '</ul>' : '') + trig + never +
      '</div>';
    host.appendChild(d);
  });
}

function renderGaps(payload, gaps) {
  const host = $('gaps');
  host.innerHTML = '';
  if (!gaps.length) {
    host.innerHTML = '<div class="empty">Nothing outstanding. Every item that evidence ' +
      'could resolve has been resolved.</div>';
    return;
  }
  const intro = document.createElement('p');
  intro.style.cssText = 'font-size:12px;color:var(--muted-foreground);margin-bottom:10px';
  intro.textContent = gaps.length + ' of 15 items were not resolved by this run. They are not ' +
    'findings of compliance and not findings of non-compliance. They are gaps in the evidence, ' +
    'and each needs closing before the submission goes out.';
  host.appendChild(intro);

  gaps.forEach((it) => {
    const d = document.createElement('details');
    d.className = 'item';
    d.innerHTML =
      '<summary>' +
        '<span class="id">' + it.item_id + '</span>' +
        '<span class="txt">' + escapeHtml(it.text) + '</span>' +
        '<span class="st st-unresolved">Not resolved</span>' +
      '</summary>' +
      '<div class="item-body">' +
        (it.notes || []).map((n) => '<div>' + escapeHtml(n) + '</div>').join('') +
      '</div>';
    host.appendChild(d);
  });
}

/* ------------------------------------------------- unanchored (§4.1.5) */

function renderUnanchored(payload) {
  const wrap = $('unanchored-wrap');
  const host = $('unanchored');
  host.innerHTML = '';

  const bad = payload.provisions.filter((p) => {
    const a = state.anchorExec[p.req_id];
    return a && !a.anchorId;
  });
  if (!bad.length) { wrap.hidden = true; return; }
  wrap.hidden = false;

  bad
    .slice()
    .sort((a, b) => (EXPOSURE_ORDER[a.exposure] - EXPOSURE_ORDER[b.exposure]) || (a.page - b.page))
    .forEach((p) => {
      const a = state.anchorExec[p.req_id];
      const el = document.createElement('div');
      el.className = 'prov unanchored';
      el.dataset.reqId = p.req_id;
      el.innerHTML =
        '<div class="prov-head">' +
          '<span class="prov-mark exp-' + p.exposure + '">' +
            '<span class="mark ' + MARKS[p.exposure] + '"></span>' +
            '<span class="prov-label">' + escapeHtml(p.exposure_label) + '</span>' +
          '</span>' +
          '<span class="prov-where">' + escapeHtml(p.req_id) + ' · ' + escapeHtml(p.where) + '</span>' +
        '</div>' +
        '<blockquote class="quote">' + escapeHtml(p.quote) + '</blockquote>' +
        '<div class="anchor-note"><b>' + escapeHtml(a.result) + '.</b> ' + escapeHtml(a.reason || '') + '</div>' +
        '<div class="prov-acts">' +
          (state.inWord ? '<button class="btn btn-secondary btn-sm act-goto">Go to clause</button>' : '') +
        '</div>';
      const goto = el.querySelector('.act-goto');
      if (goto) goto.onclick = () => goToClause(p);
      host.appendChild(el);
    });
}

/* --------------------------------------------------------- minimap (§3.5) */

function renderMinimap(payload) {
  const host = $('minimap');
  host.innerHTML = '';
  const anchored = payload.provisions.filter((p) => {
    const a = state.anchorExec[p.req_id];
    return a && a.anchorId;
  });
  if (!anchored.length) { host.hidden = true; return; }
  host.hidden = false;

  const maxPage = Math.max(1, (payload.stats && payload.stats.pages) || 1);
  anchored.forEach((p) => {
    const tick = document.createElement('div');
    tick.className = 'minimap-tick exp-' + p.exposure;
    if (state.reviewedState[p.req_id]) tick.classList.add('reviewed');
    if (!matchesFilters(p)) tick.classList.add('dimmed');
    const pct = maxPage > 1 ? ((p.page - 1) / (maxPage - 1)) * 100 : 0;
    tick.style.top = pct + '%';
    tick.title = p.req_id + ' · p.' + p.page;
    tick.onclick = () => walkToReqId(p.req_id);
    host.appendChild(tick);
  });
  updateMinimapCursor();
}

function updateMinimapCursor() {
  const host = $('minimap');
  if (host.hidden) return;
  let cursor = host.querySelector('.minimap-cursor');
  if (state.walkIndex < 0 || !state.walkOrder.length) {
    if (cursor) cursor.remove();
    return;
  }
  const p = provisionsById()[state.walkOrder[state.walkIndex]];
  if (!p) return;
  if (!cursor) {
    cursor = document.createElement('div');
    cursor.className = 'minimap-cursor';
    host.appendChild(cursor);
  }
  const maxPage = Math.max(1, (state.review.stats && state.review.stats.pages) || 1);
  cursor.style.top = (maxPage > 1 ? ((p.page - 1) / (maxPage - 1)) * 100 : 0) + '%';
}

/* ----------------------------------------------------------- walk (§6.2) */

function computeWalkOrder(payload) {
  return payload.provisions
    .filter((p) => matchesFilters(p))
    .slice()
    .sort((a, b) => (EXPOSURE_ORDER[a.exposure] - EXPOSURE_ORDER[b.exposure]) || (a.page - b.page))
    .map((p) => p.req_id);
}

function updateWalkBar() {
  if (!state.review || !state.review.provisions.length) return;
  $('walk-pos').textContent = state.walkIndex >= 0
    ? (state.walkIndex + 1) + ' / ' + state.walkOrder.length
    : '— / ' + state.walkOrder.length;
}

function scrollCardIntoView(reqId) {
  document.querySelectorAll('.prov.walk-current').forEach((el) => el.classList.remove('walk-current'));
  const card = document.querySelector('.prov[data-req-id="' + CSS.escape(reqId) + '"]');
  if (card) {
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('walk-current');
  }
}

async function walkTo(index) {
  if (!state.walkOrder.length) return;
  state.walkIndex = ((index % state.walkOrder.length) + state.walkOrder.length) % state.walkOrder.length;
  const reqId = state.walkOrder[state.walkIndex];
  updateWalkBar();
  scrollCardIntoView(reqId);
  updateMinimapCursor();
  await selectInDocument(reqId);
}

function walkToReqId(reqId) {
  const idx = state.walkOrder.indexOf(reqId);
  walkTo(idx >= 0 ? idx : 0);
}

async function walkConfirmNext() {
  if (state.walkIndex < 0) { await walkTo(0); return; }
  const reqId = state.walkOrder[state.walkIndex];
  const p = provisionsById()[reqId];
  if (p && !state.reviewedState[reqId]) {
    const card = document.querySelector('.prov[data-req-id="' + CSS.escape(reqId) + '"]');
    await decide(p, 'confirm', card);
  }
  await walkTo(state.walkIndex + 1);
}

/* -------------------------------------------------------- filters (§6.3) */

function matchesFilters(p) {
  const f = state.filters;
  if (f.bands.size && !f.bands.has(p.exposure)) return false;
  if (f.category && p.category !== f.category) return false;
  if (f.item && !(p.checklist_items || []).map(String).includes(f.item)) return false;
  const reviewed = !!state.reviewedState[p.req_id];
  if (f.reviewed === 'reviewed' && !reviewed) return false;
  if (f.reviewed === 'unreviewed' && reviewed) return false;
  return true;
}

function renderFilterBar(payload) {
  const bandHost = $('filter-band');
  bandHost.innerHTML = '';
  ['high', 'unclassified', 'open', 'routine'].forEach((band) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip exp-' + band;
    chip.setAttribute('aria-pressed', String(state.filters.bands.has(band)));
    chip.innerHTML = '<span class="mark ' + MARKS[band] + '"></span>' + BAND_LABEL[band];
    chip.onclick = () => {
      if (state.filters.bands.has(band)) state.filters.bands.delete(band);
      else state.filters.bands.add(band);
      chip.setAttribute('aria-pressed', String(state.filters.bands.has(band)));
      applyFilters();
    };
    bandHost.appendChild(chip);
  });

  const cats = Array.from(new Set(payload.provisions.map((p) => p.category))).sort();
  $('filter-category').innerHTML = '<option value="">All categories</option>' +
    cats.map((c) => '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>').join('');

  const items = Array.from(new Set(
    payload.provisions.flatMap((p) => p.checklist_items || [])
  )).sort((a, b) => a - b);
  $('filter-item').innerHTML = '<option value="">All checklist items</option>' +
    items.map((i) => '<option value="' + i + '">Item ' + i + '</option>').join('');
}

async function applyFilters() {
  document.querySelectorAll('.prov[data-req-id]').forEach((el) => {
    const p = provisionsById()[el.dataset.reqId];
    if (p) el.hidden = !matchesFilters(p);
  });
  state.walkOrder = computeWalkOrder(state.review);
  if (state.walkIndex >= state.walkOrder.length) state.walkIndex = -1;
  updateWalkBar();
  renderMinimap(state.review);
  if (state.inWord && Object.keys(state.anchorExec).length) {
    await applyFiltersToDocument();
  }
}

async function applyFiltersToDocument() {
  try {
    await Word.run(async (context) => {
      const ccs = context.document.contentControls;
      ccs.load('items/tag');
      await context.sync();
      const byId = provisionsById();
      const reveal = state.markupMode === 'comments'
        ? Word.ContentControlAppearance.hidden : Word.ContentControlAppearance.boundingBox;
      ccs.items.forEach((cc) => {
        const parsed = parseTag(cc.tag);
        if (!parsed) return;
        const anyMatch = parsed.reqIds.some((id) => byId[id] && matchesFilters(byId[id]));
        cc.appearance = anyMatch ? reveal : Word.ContentControlAppearance.hidden;
      });
      await context.sync();
    });
  } catch (e) { /* the pane is still correct even if the document side failed */ }
}

function clearFilters() {
  state.filters = { bands: new Set(), category: '', item: '', reviewed: '' };
  $('filter-category').value = '';
  $('filter-item').value = '';
  $('filter-reviewed').value = '';
  document.querySelectorAll('#filter-band .chip').forEach((c) => c.setAttribute('aria-pressed', 'false'));
  applyFilters();
}

/* ------------------------------------------------------- reviewed (§3.4) */

function toggleShowReviewed() {
  state.showReviewed = $('chk-show-reviewed').checked;
  document.querySelectorAll('.prov[data-req-id]').forEach((el) => {
    el.classList.toggle('reviewed', !state.showReviewed && !!state.reviewedState[el.dataset.reqId]);
  });
  if (state.inWord) repaintAllControls();
}

/* ------------------------------------------------- the document, in Word */

/** Word search takes no more than 255 characters and does not cross a
 *  paragraph. Take a distinctive run from inside the quote and shorten it until
 *  Word finds it, rather than asking for the whole provision and getting nothing.
 *  Kept as the fallback for "Go to clause" / walk mode on provisions that were
 *  never anchored (or before any markup has been run) — the anchor plan
 *  (anchor.js) is the primary, verified path once markup exists. */
function searchFragments(quote) {
  const flat = String(quote).replace(/\s+/g, ' ').trim()
    .replace(/[\^]/g, ' ');              // ^ is an escape character in Word's find
  const out = [];
  [SEARCH_MAX, 120, 70, 40].forEach((n) => {
    if (flat.length <= n) {
      if (!out.includes(flat)) out.push(flat);
      return;
    }
    let cut = flat.slice(0, n);
    cut = cut.slice(0, cut.lastIndexOf(' ') > 20 ? cut.lastIndexOf(' ') : n);
    if (cut && !out.includes(cut)) out.push(cut);
  });
  return out;
}

async function findRange(context, quote) {
  for (const frag of searchFragments(quote)) {
    const results = context.document.body.search(frag, { matchCase: false, ignorePunct: true });
    results.load('items');
    await context.sync();
    if (results.items.length) return results.items[0];
  }
  return null;
}

/** A control's tag is `CGATE:<req_id>[+<req_id>…]:<band>` (§5.3). */
function parseTag(tag) {
  const m = /^CGATE:([^:]+):([a-z]+)$/.exec(tag || '');
  if (!m) return null;
  return { reqIds: m[1].split('+'), band: m[2] };
}

async function findAnchoredRange(context, reqId) {
  const ccs = context.document.contentControls;
  ccs.load('items/tag');
  await context.sync();
  const cc = ccs.items.find((c) => {
    const parsed = parseTag(c.tag);
    return parsed && parsed.reqIds.indexOf(reqId) !== -1;
  });
  return cc || null;
}

async function selectInDocument(reqId) {
  if (!state.inWord) return;
  const p = provisionsById()[reqId];
  if (!p) return;
  try {
    await Word.run(async (context) => {
      let range = await findAnchoredRange(context, reqId);
      if (!range) range = await findRange(context, p.quote);
      if (!range) {
        say('Could not locate ' + escapeHtml(reqId) + ' in the open document.', true);
        return;
      }
      range.select();
      await context.sync();
      say('Selected ' + escapeHtml(reqId) + ' at ' + escapeHtml(p.where) + '.');
    });
  } catch (e) {
    say('Word refused the selection: ' + escapeHtml(e.message), true);
  }
}

async function goToClause(p) {
  if (!state.inWord) return;
  try {
    await Word.run(async (context) => {
      const range = (await findAnchoredRange(context, p.req_id)) || (await findRange(context, p.quote));
      if (!range) {
        say('Could not locate that wording in the open document. It is quoted verbatim ' +
            'from p.' + p.page + ' of the file that was reviewed — if Word has since ' +
            'changed, the two are no longer the same document.', true);
        return;
      }
      range.select();
      await context.sync();
      say('Selected ' + escapeHtml(p.req_id) + ' at ' + escapeHtml(p.where) + '.');
    });
  } catch (e) {
    say('Word refused the selection: ' + escapeHtml(e.message), true);
  }
}

/* ------------------------------------------------- control appearance */

function controlTitle(reqIds, byId) {
  const members = reqIds.map((id) => byId[id]).filter(Boolean);
  if (!members.length) return 'CGATE';
  members.sort((a, b) => EXPOSURE_ORDER[a.exposure] - EXPOSURE_ORDER[b.exposure]);
  const w = members[0];
  let t = '[' + w.category + ' · ' + w.req_id + '] ' + w.exposure_label + ' — ' +
    (ACTOR_SHORT[w.actor] || 'actor unresolved');
  if (members.length > 1) t += ' (+' + (members.length - 1) + ' more)';
  return t.slice(0, 250);
}

function recomputeControlAppearance(reqIds, band, byId) {
  const allReviewed = reqIds.length > 0 && reqIds.every((id) => state.reviewedState[id]);
  if (allReviewed && !state.showReviewed) {
    const parts = reqIds.map((id) => reviewedLabel(state.reviewedState[id]));
    return { color: REVIEWED_HEX, title: ('Reviewed · ' + parts.join('; ')).slice(0, 250) };
  }
  return { color: BAND_HEX[band] || BAND_HEX.routine, title: controlTitle(reqIds, byId) };
}

async function repaintControl(reqId) {
  if (!state.inWord) return;
  try {
    await Word.run(async (context) => {
      const cc = await findAnchoredRange(context, reqId);
      if (!cc) return;
      const parsed = parseTag(cc.tag);
      if (!parsed) return;
      const appearance = recomputeControlAppearance(parsed.reqIds, parsed.band, provisionsById());
      cc.color = appearance.color;
      cc.title = appearance.title;
      await context.sync();
    });
  } catch (e) { /* the pane's own state is still correct */ }
}

async function repaintAllControls() {
  try {
    await Word.run(async (context) => {
      const ccs = context.document.contentControls;
      ccs.load('items/tag');
      await context.sync();
      const byId = provisionsById();
      ccs.items.forEach((cc) => {
        const parsed = parseTag(cc.tag);
        if (!parsed) return;
        const appearance = recomputeControlAppearance(parsed.reqIds, parsed.band, byId);
        cc.color = appearance.color;
        cc.title = appearance.title;
      });
      await context.sync();
    });
  } catch (e) { /* cosmetic only */ }
}

/* -------------------------------------------------- Track Changes (§5.2) */

async function checkTrackChanges(context) {
  context.document.load('changeTrackingMode');
  await context.sync();
  if (context.document.changeTrackingMode === Word.ChangeTrackingMode.off) return null;

  const restore = context.document.changeTrackingMode;
  const ok = await ask(askHost(), {
    text: 'Track Changes is on.',
    detail: 'Inserted markup would show as tracked revisions. Turn it off while marking '
          + 'up, then restore it afterwards?',
    ok: 'Turn off and mark up',
    cancel: 'Leave it on',
  });
  if (!ok) throw new Error('cancelled — Track Changes is on');
  context.document.changeTrackingMode = Word.ChangeTrackingMode.off;
  await context.sync();
  return restore;
}

/* ---------------------------------------------- restore journal (§5.3) */

var JOURNAL_NS = 'http://cgate.example/cgate/journal';

function journalXml(entries) {
  const rows = entries.map((e) =>
    '<e t="' + String(e.tag).replace(/&/g, '&amp;').replace(/"/g, '&quot;') +
    '" u="' + String(e.underline) + '"/>'
  ).join('');
  return '<j xmlns="' + JOURNAL_NS + '">' + rows + '</j>';
}

async function writeJournal(root, context, entries) {
  const existing = root.customXmlParts.getByNamespace(JOURNAL_NS);
  existing.load('items');
  await context.sync();
  existing.items.forEach((p) => p.delete());
  root.customXmlParts.add(journalXml(entries));
  await context.sync();
}

/** The inverse of journalXml(). Pulled out as its own pure function so the
 *  encode/decode round-trip is testable without Word — see
 *  tests/test_markup_roundtrip.py. */
function parseJournalXml(xml) {
  const entries = [];
  const re = /<e t="([^"]*)" u="([^"]*)"\s*\/>/g;
  let m;
  while ((m = re.exec(xml))) {
    entries.push({ tag: m[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'), underline: m[2] });
  }
  return entries;
}

async function readJournal(root, context) {
  const existing = root.customXmlParts.getByNamespace(JOURNAL_NS);
  existing.load('items');
  await context.sync();
  if (!existing.items.length) return [];
  const result = existing.items[0].getXml();
  await context.sync();
  return parseJournalXml(result.value);
}

async function clearJournal(root, context) {
  const existing = root.customXmlParts.getByNamespace(JOURNAL_NS);
  existing.load('items');
  await context.sync();
  existing.items.forEach((p) => p.delete());
  await context.sync();
}

/* ------------------------------------------------------- mark up (§5, §6.4) */

/** The margin note. Built from the same fields the card renders, so what is in
 *  the document and what is in the pane can never disagree. The hash prefix
 *  (§6.4) makes a printed comment traceable to the exact bytes reviewed. */
function commentBody(p, docId) {
  const L = [];
  const hashPrefix = docId ? docId.slice(0, 12) + ' · ' : '';
  L.push(COMMENT_MARK + ' ' + hashPrefix + p.req_id + ' · ' + p.exposure_label.toUpperCase() + ' · ' + p.where);
  L.push('');
  L.push('BINDS: ' + p.binds);
  L.push('');
  L.push('READS AGAINST US: ' + p.against);
  if (p.manageable) { L.push(''); L.push('MANAGEABLE BECAUSE: ' + p.manageable); }
  L.push('');
  L.push('HOW THIS WAS FOUND: ' + p.found);
  if (p.checklist_items && p.checklist_items.length) {
    L.push('');
    L.push('BEARS ON CHECKLIST ITEM(S): ' + p.checklist_items.join(', ') + ' of SOP-3.11.');
  }
  L.push('');
  L.push('Reply Confirm / Reject: <reason> / Classify: R<n> to record a decision from here.');
  L.push('');
  L.push('Not a determination of the Compliance Officer. The system gathers evidence; ' +
         'a named human decides. ' +
         'Governed by the cgate review-process notes.');
  return L.join('\n');
}

/** The core of "Mark up the document": plan + execute anchors, insert one
 *  content control per coalesced anchor (§4.1.4), one comment per member
 *  (§4.1.4 — "each member keeps its own comment"), apply the mode's visual
 *  treatment, and (Marked up mode) journal the underline it overwrote.
 *  `root` is `context.document` (this document) or a not-yet-opened
 *  `Document` proxy from `Application.createDocument` (a copy, §5.1) — every
 *  operation below chains off `root`, never off `context.document` directly,
 *  so the same code marks up either target. */
async function markUpContext(context, root, mode, review, isThisDoc) {
  const existing = root.contentControls;
  existing.load('items/tag');
  await context.sync();
  const already = existing.items.filter((c) => (c.tag || '').indexOf('CGATE:') === 0);
  if (already.length) {
    throw new Error(
      already.length + ' CGATE control(s) are already present in this document. ' +
      'Remove markup first, then mark up again — never nested, never duplicated.'
    );
  }

  let trackRestore = null;
  if (isThisDoc && state.canComment) {
    trackRestore = await checkTrackChanges(context);
  }

  busy('Planning anchors …');
  const { plan, executed } = await CGATE_ANCHOR.planAndAnchor(
    context, root, API, review.doc_id,
    (done, total) => busy('Anchoring … ' + done + '/' + total)
  );

  const byId = {};
  review.provisions.forEach((p) => { byId[p.req_id] = p; });

  const journalEntries = [];
  let placed = 0;
  let idx = 0;

  for (const a of plan.anchors) {
    idx += 1;
    busy('Marking up … ' + idx + '/' + plan.anchors.length);

    const primary = executed[a.req_ids[0]];
    if (!primary || !primary.range) continue;

    const cc = primary.range.insertContentControl();
    cc.tag = 'CGATE:' + a.req_ids.join('+') + ':' + a.band;
    cc.title = controlTitle(a.req_ids, byId);
    cc.color = BAND_HEX[a.band] || BAND_HEX.routine;
    cc.cannotDelete = false;
    cc.appearance = mode === 'comments'
      ? Word.ContentControlAppearance.hidden : Word.ContentControlAppearance.boundingBox;

    if (mode === 'marked-up') {
      cc.load('font/underline');
      await context.sync();
      journalEntries.push({ tag: cc.tag, underline: cc.font.underline });
      cc.font.underline = BAND_UNDERLINE[a.band] || 'None';
    }

    try { context.trackedObjects.add(cc); cc.onEntered.add(handleControlEntered); }
    catch (e) { /* §6.1 sync is best-effort; frames/comments still work without it */ }

    a.req_ids.forEach((reqId) => {
      const p = byId[reqId];
      if (p) cc.getRange().insertComment(commentBody(p, review.doc_id));
    });

    await context.sync();
    placed += a.req_ids.length;
  }

  if (mode === 'marked-up' && journalEntries.length) {
    await writeJournal(root, context, journalEntries);
  }

  if (state.canStampProvenance) {
    try {
      const props = root.properties.customProperties;
      props.add('CGATE_DocHash', review.doc_id);
      props.add('CGATE_LexiconVersion', String(review.lexicon_version || ''));
      props.add('CGATE_ReviewedAt', new Date().toISOString());
      props.add('CGATE_ToolVersion', String(review.tool_version || ''));
      await context.sync();
    } catch (e) { /* provenance is best-effort; the markup itself already succeeded */ }
  }

  if (trackRestore) {
    context.document.changeTrackingMode = trackRestore;
    await context.sync();
  }

  state.anchorExec = executed;
  return { plan, executed, placed, missed: plan.stats.total - placed };
}

/** §6.1 — put the cursor in a marked clause and the pane scrolls that
 *  provision's card into view. UNVERIFIED against live Word: content-control
 *  event registration is the one piece of this build with no automated
 *  coverage and no way to exercise it outside Word — if the pane ever fails
 *  to scroll on cursor entry, this is the first place to look. Everything
 *  else (frames, comments, removal, filters, walk mode) degrades cleanly if
 *  this handler never fires; it is wrapped so a failure here cannot break
 *  markup itself. */
async function handleControlEntered(event) {
  try {
    await Word.run(async (context) => {
      const id = event && event.ids && event.ids[0];
      if (id === undefined || id === null) return;
      const cc = context.document.contentControls.getById(id);
      cc.load('tag');
      await context.sync();
      const parsed = parseTag(cc.tag);
      if (!parsed) return;
      scrollCardIntoView(parsed.reqIds[0]);
    });
  } catch (e) { /* best-effort sync only */ }
}

async function markUpAsCopy(mode) {
  busy('Reading the document from Word …');
  const bytes = await documentBytes();
  const b64 = bytesToBase64(bytes);
  let out;
  await Word.run(async (context) => {
    const doc = context.application.createDocument(b64);
    out = await markUpContext(context, doc, mode, state.review, false);
    doc.open();
    await context.sync();
  });
  return out;
}

async function annotate() {
  if (!state.inWord || !state.review) return;
  if (!state.canComment) {
    say('This build of Word does not expose the comments API (WordApi 1.4). ' +
        '“Go to clause” still works on every provision.', true);
    return;
  }

  const target = $('markup-target').value;
  const mode = $('markup-mode').value;
  state.markupTarget = target;
  state.markupMode = mode;

  if (mode === 'marked-up' && !state.canCustomXml) {
    say('Marked up mode needs Word’s custom XML parts (WordApi 1.4) for its restore journal, ' +
        'not available on this host. Use Frames or Comments only instead.', true);
    return;
  }

  const provisions = state.review.provisions;
  const ok = await ask(askHost(), {
    text: (target === 'copy' ? 'Open a marked-up copy' : 'Mark up THIS document')
        + ' with up to ' + provisions.length + ' framed clause(s)?',
    detail: 'Every control is tagged ' + COMMENT_MARK + ': and every comment starts '
          + COMMENT_MARK + ' — Remove markup deletes exactly those and nothing else.'
          + (target === 'this' ? ' This changes the file the firm is bidding against.' : ''),
    ok: target === 'copy' ? 'Open the copy' : 'Mark up this file',
    cancel: 'Not now',
    danger: target === 'this',
  });
  if (!ok) return;

  $('btn-annotate').disabled = true;
  try {
    const result = target === 'copy'
      ? await markUpAsCopy(mode)
      : await Word.run((context) => markUpContext(context, context.document, mode, state.review, true));

    renderProvisions(state.review);
    say('Marked up ' + result.placed + ' provision(s) across ' + result.plan.anchors.length +
        ' frame(s)' + (result.missed ? '; ' + result.missed + ' listed under Unanchored below' : '') + '.');
  } catch (e) {
    say('Markup stopped: ' + escapeHtml(e.message), true);
  } finally {
    $('btn-annotate').disabled = false;
  }
}

/* ---------------------------------------------------- reply grammar (§6.5) */

/** Every reply is forwarded to the host, which owns the grammar (it has to be
 *  testable without Word — see cgate/feedback.py parse_reply). This function
 *  only walks the document's comments and their replies. */
async function ingestReplies(context) {
  const comments = context.document.body.getComments();
  comments.load('items/content/id');
  await context.sync();
  const ours = comments.items.filter((c) => (c.content || '').indexOf(COMMENT_MARK + ' ') === 0);

  let ingested = 0;
  const unread = [];
  const withReplies = [];

  for (const c of ours) {
    c.replies.load('items/text/id/authorName');
    await context.sync();
    if (c.replies.items.length) withReplies.push(c);

    for (const reply of c.replies.items) {
      const key = c.id + ':' + reply.id;
      if (state.ingestedReplyKeys.has(key)) continue;
      state.ingestedReplyKeys.add(key);

      try {
        const r = await fetch(API + '/api/feedback/reply', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            doc_id: state.review.doc_id,
            reviewer: reply.authorName || 'unknown',
            comment_content: c.content,
            reply_text: reply.text,
          }),
        });
        const j = await r.json();
        if (j.parsed) {
          ingested += 1;
          state.reviewedState[j.req_id] = {
            kind: j.kind, reviewer: reply.authorName || 'unknown', category: j.category, ts: Date.now(),
          };
          const card = document.querySelector('.prov[data-req-id="' + CSS.escape(j.req_id) + '"]');
          paintCardReviewed(card, state.reviewedState[j.req_id]);
          await repaintControl(j.req_id);
        } else {
          unread.push({ comment: (c.content || '').split('\n')[0], reply: reply.text, reason: j.reason });
        }
      } catch (e) {
        unread.push({ comment: (c.content || '').split('\n')[0], reply: reply.text, reason: e.message });
      }
    }
  }

  renderMinimap(state.review);
  return { ingested, unread, withReplies };
}

/* An unread reply is a reviewer saying something the reply grammar could not
 * parse into Confirm or Reject. It is never dropped and never guessed at. It
 * used to arrive as an alert — a wall of text, dismissed once, unrecoverable.
 * It stays on the pane now until the reviewer takes it away, with the reply
 * verbatim, because the words are the evidence. */
function renderUnreadReplies(list) {
  const host = askHost();
  const prev = host.querySelector('.unread');
  if (prev) prev.remove();
  if (!list.length) return;

  const box = document.createElement('div');
  box.className = 'ask unread';
  box.innerHTML =
    '<p class="ask-q">' + list.length + ' repl' + (list.length === 1 ? 'y' : 'ies') +
      ' not recorded, and not deleted</p>' +
    '<p class="ask-detail">Neither Confirm nor Reject. Read them and reply again in the ' +
      'document, or act on them here.</p>' +
    '<ul class="unread-list">' +
    list.map((u) =>
      '<li><span class="code">' + escapeHtml(u.comment) + '</span>' +
      '<blockquote class="quote">' + escapeHtml(u.reply) + '</blockquote>' +
      '<span class="ask-detail">' + escapeHtml(u.reason) + '</span></li>').join('') +
    '</ul>' +
    '<div class="ask-acts"><button type="button" class="btn btn-secondary btn-sm unread-ok">' +
      'Dismiss</button></div>';

  host.appendChild(box);
  box.querySelector('.unread-ok').onclick = () => box.remove();
  if (box.scrollIntoView) box.scrollIntoView({ block: 'nearest' });
}

async function syncReplies() {
  if (!state.inWord) return;
  $('btn-sync-replies').disabled = true;
  try {
    const result = await Word.run((context) => ingestReplies(context));
    say(result.ingested + ' repl(y/ies) recorded.' +
        (result.unread.length ? ' ' + result.unread.length + ' did not parse — see the alert.' : ''));
    renderUnreadReplies(result.unread);
  } catch (e) {
    say('Could not sync replies: ' + escapeHtml(e.message), true);
  } finally {
    $('btn-sync-replies').disabled = false;
  }
}

/* --------------------------------------------------- remove markup (§5.3) */

async function removeMarkup(context) {
  const root = context.document;

  // Ingest every human reply before anything is deleted (§5.3 — "never
  // delete an uningested reply").
  const replyResult = await ingestReplies(context);

  const journalEntries = state.canCustomXml ? await readJournal(root, context) : [];
  const journalByTag = {};
  journalEntries.forEach((e) => { journalByTag[e.tag] = e.underline; });

  const ccs = root.contentControls;
  ccs.load('items/tag');
  await context.sync();
  const ours = ccs.items.filter((c) => (c.tag || '').indexOf('CGATE:') === 0);

  let restoredUnderline = 0;
  ours.forEach((cc) => {
    const prior = journalByTag[cc.tag];
    if (prior !== undefined) {
      cc.getRange().font.underline = prior;
      restoredUnderline += 1;
    }
  });
  if (restoredUnderline) await context.sync();

  // keepContent = true: delete the wrapper control, never the clause text it
  // wraps (§0.3, §5.3 — the contract is not the tool's to edit).
  ours.forEach((c) => c.delete(true));
  await context.sync();

  const comments = root.body.getComments();
  comments.load('items/content/id');
  await context.sync();
  const ourComments = comments.items.filter((c) => (c.content || '').indexOf(COMMENT_MARK + ' ') === 0);

  let deleteReplied = true;
  if (replyResult.withReplies.length) {
    const a = await ask(askHost(), {
      text: replyResult.withReplies.length + ' thread(s) here carry reviewer replies.',
      detail: 'Every reply has just been recorded to the ledger. Delete these threads too, '
            + 'or leave them in the document to be read again?',
      ok: 'Delete them',
      cancel: 'Leave them',
    });
    deleteReplied = !!a;
  }

  let removed = 0;
  const repliedIds = replyResult.withReplies.map((c) => c.id);
  ourComments.forEach((c) => {
    if (repliedIds.indexOf(c.id) !== -1 && !deleteReplied) return;
    c.delete();
    removed += 1;
  });
  await context.sync();

  if (state.canCustomXml) await clearJournal(root, context);

  state.anchorExec = {};
  return { removedControls: ours.length, removedComments: removed, restoredUnderline, replyResult };
}

async function removeAnnotations() {
  if (!state.inWord) return;
  if (!state.canComment) { say('No comments API in this build of Word.', true); return; }
  $('btn-unannotate').disabled = true;
  try {
    const result = await Word.run((context) => removeMarkup(context));
    say('Removed ' + result.removedControls + ' frame(s) and ' + result.removedComments + ' comment(s). ' +
        (result.replyResult.ingested ? result.replyResult.ingested + ' reply decision(s) recorded first. ' : '') +
        'Any other comments in the document were left alone.');
    renderProvisions(state.review);
    renderUnreadReplies(result.replyResult.unread);
  } catch (e) {
    say('Could not remove markup: ' + escapeHtml(e.message), true);
  } finally {
    $('btn-unannotate').disabled = false;
  }
}

/* -------------------------------------------------------------- schedule */

/** §6.4 — a Schedule of Provisions as a *separate* document. Never inserted
 *  into the contract. */
async function generateSchedule() {
  if (!state.inWord || !state.review) return;
  $('btn-schedule').disabled = true;
  try {
    busy('Building the schedule of provisions …');
    await Word.run(async (context) => {
      const doc = context.application.createDocument();
      const body = doc.body;

      const heading = body.insertParagraph('Schedule of Provisions', Word.InsertLocation.end);
      heading.font.bold = true;
      heading.font.size = 16;
      const sub = body.insertParagraph(
        state.review.doc_name + ' · sha256 ' + state.review.doc_id.slice(0, 16) + '… · ' +
        state.review.provisions.length + ' provisions',
        Word.InsertLocation.end
      );
      sub.font.size = 9;
      sub.font.color = '#4a5578';

      const ordered = state.review.provisions.slice().sort((a, b) =>
        (EXPOSURE_ORDER[a.exposure] - EXPOSURE_ORDER[b.exposure]) || (a.page - b.page));
      const rows = [['Ref', 'Band', 'Page', 'Clause', 'Category', 'Checklist item(s)']];
      ordered.forEach((p) => {
        rows.push([
          p.req_id, p.exposure_label, String(p.page), p.clause_number || '—',
          p.category, (p.checklist_items || []).join(', ') || '—',
        ]);
      });
      const table = body.insertTable(rows.length, rows[0].length, Word.InsertLocation.end, rows);
      table.headerRowCount = 1;

      if (state.canStampProvenance) {
        const props = doc.properties.customProperties;
        props.add('CGATE_DocHash', state.review.doc_id);
        props.add('CGATE_LexiconVersion', String(state.review.lexicon_version || ''));
        props.add('CGATE_ReviewedAt', new Date().toISOString());
        props.add('CGATE_ToolVersion', String(state.review.tool_version || ''));
      }

      doc.open();
      await context.sync();
    });
    say('Schedule of provisions opened as a separate document. The document under review was not modified.');
  } catch (e) {
    say('Could not build the schedule: ' + escapeHtml(e.message), true);
  } finally {
    $('btn-schedule').disabled = false;
  }
}

/* ----------------------------------------------------------- inline asks */

/* Word's task pane is a document surface. A `window.confirm` in it is the one
 * thing that cannot be mistaken for Word: it steals focus, it cannot show the
 * clause it is asking about, its buttons say OK and Cancel whatever the question
 * was, and on some Office hosts it is suppressed outright — which would turn a
 * refusal to proceed into a silent success. PLAN §0.4 says the tool speaks
 * through Word's own idioms; seven browser modals were the loudest exception.
 *
 * `ask()` renders the question where the answer belongs — inside the card, or
 * above the button that raised it — and resolves to the field values, or to
 * null if the reviewer backs out. Nothing is recorded on null.
 */
function ask(host, spec) {
  return new Promise((resolve) => {
    const prev = host.querySelector('.ask');
    if (prev) prev.remove();

    const box = document.createElement('div');
    box.className = 'ask' + (spec.danger ? ' ask-danger' : '');

    const fields = (spec.fields || []).map((f, i) => {
      const id = 'ask-f-' + i;
      if (f.type === 'choice') {
        return '<div class="ask-field"><label for="' + id + '">' + escapeHtml(f.label) + '</label>' +
          '<div class="ask-choices" data-for="' + id + '">' +
          f.options.map((o) =>
            '<button type="button" class="chip ask-choice' +
            (o === f.value ? ' is-on' : '') + '" data-v="' + escapeHtml(o) + '">' +
            escapeHtml(o) + '</button>').join('') +
          '</div><input type="hidden" id="' + id + '" value="' + escapeHtml(f.value || '') + '" /></div>';
      }
      const tag = f.type === 'text' ? 'textarea' : 'input';
      return '<div class="ask-field"><label for="' + id + '">' + escapeHtml(f.label) + '</label>' +
        '<' + tag + ' id="' + id + '" class="ask-input"' +
        (f.type === 'text' ? ' rows="2"' : ' type="text"') +
        (f.list ? ' list="' + escapeHtml(f.list) + '"' : '') +
        (f.placeholder ? ' placeholder="' + escapeHtml(f.placeholder) + '"' : '') +
        (f.type === 'text' ? '>' + escapeHtml(f.value || '') + '</textarea>'
                           : ' value="' + escapeHtml(f.value || '') + '" />') +
        '</div>';
    }).join('');

    box.innerHTML =
      '<p class="ask-q">' + escapeHtml(spec.text) + '</p>' +
      (spec.detail ? '<p class="ask-detail">' + escapeHtml(spec.detail) + '</p>' : '') +
      fields +
      '<div class="ask-acts">' +
        '<button type="button" class="btn btn-primary btn-sm ask-ok">' +
          escapeHtml(spec.ok || 'OK') + '</button>' +
        '<button type="button" class="btn btn-secondary btn-sm ask-no">' +
          escapeHtml(spec.cancel || 'Cancel') + '</button>' +
      '</div>';

    host.appendChild(box);

    box.querySelectorAll('.ask-choices').forEach((group) => {
      const hidden = box.querySelector('#' + group.dataset.for);
      group.querySelectorAll('.ask-choice').forEach((b) => {
        b.onclick = () => {
          group.querySelectorAll('.ask-choice').forEach((x) => x.classList.remove('is-on'));
          b.classList.add('is-on');
          hidden.value = b.dataset.v;
        };
      });
    });

    const done = (result) => { box.remove(); resolve(result); };

    box.querySelector('.ask-ok').onclick = () => {
      const values = {};
      (spec.fields || []).forEach((f, i) => {
        values[f.name] = (box.querySelector('#ask-f-' + i).value || '').trim();
      });
      done(values);
    };
    box.querySelector('.ask-no').onclick = () => done(null);
    box.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); done(null); }
      if (ev.key === 'Enter' && ev.target.tagName !== 'TEXTAREA') {
        ev.preventDefault();
        box.querySelector('.ask-ok').click();
      }
    });

    const first = box.querySelector('.ask-input, .ask-choice, .ask-ok');
    if (first) first.focus();
    if (box.scrollIntoView) box.scrollIntoView({ block: 'nearest' });
  });
}

/** The pane-level slot, for questions that are about the document rather than
 *  about one provision. Sits above the actions that raise it. */
function askHost() {
  let host = $('ask-slot');
  if (!host) {
    host = document.createElement('div');
    host.id = 'ask-slot';
    const main = document.querySelector('main');
    main.insertBefore(host, main.firstChild);
  }
  return host;
}

/* -------------------------------------------------------------- feedback */

async function reviewerName(host) {
  let who = '';
  try { who = window.localStorage.getItem('cgate.reviewer') || ''; } catch (e) { /* private mode */ }
  if (who) return who;

  const a = await ask(host || askHost(), {
    text: 'Your name, for the calibration ledger.',
    detail: 'Every decision is recorded against the person who made it. An anonymous '
          + 'entry cannot be audited and will not be recorded.',
    fields: [{ name: 'who', label: 'Name', list: 'reviewers', placeholder: 'e.g. A. Reviewer' }],
    ok: 'Use this name',
  });
  who = a ? (a.who || '').trim() : '';
  if (who) {
    try { window.localStorage.setItem('cgate.reviewer', who); } catch (e) { /* ignore */ }
    rememberReviewer(who);
  }
  return who;
}

/** Feeds the <datalist> the pane has always carried and never populated. */
function rememberReviewer(who) {
  const list = $('reviewers');
  if (!list || Array.from(list.options || []).some((o) => o.value === who)) return;
  const opt = document.createElement('option');
  opt.value = who;
  list.appendChild(opt);
}

async function recordDecision(p, kind, reviewer, reason, category, card) {
  try {
    const r = await fetch(API + '/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        doc_id: state.review.doc_id, req_id: p.req_id, kind, reviewer, reason,
        category: category || undefined, source: 'pane',
      }),
    });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* keep statusText */ }
      throw new Error(detail);
    }
    const j = await r.json();

    state.reviewedState[p.req_id] = { kind, reviewer, category: category || null, ts: Date.now() };
    paintCardReviewed(card || document.querySelector('.prov[data-req-id="' + CSS.escape(p.req_id) + '"]'),
      state.reviewedState[p.req_id]);
    await repaintControl(p.req_id);
    renderMinimap(state.review);

    const calCat = category || p.category;
    const c = (j.calibration || {})[calCat];
    say('Recorded.' + (c && c.precision != null
      ? ' ' + calCat + ' precision is now ' + Math.round(c.precision * 100) + '% over ' +
        (c.confirmed + c.rejected) + ' decision(s).'
      : ' No precision figure for ' + calCat + ' yet — it needs more decisions.'));
  } catch (e) {
    say('Not recorded: ' + escapeHtml(e.message), true);
  }
}

async function decide(p, kind, card) {
  const host = card || askHost();
  const reviewer = await reviewerName(host);
  if (!reviewer) { say('Not recorded — a decision needs a name.', true); return; }

  let reason = '';
  if (kind === 'reject') {
    const a = await ask(host, {
      text: 'Why is ' + p.req_id + ' wrong?',
      detail: 'It matched on: ' + ((p.matched || []).join(', ') || 'nothing recorded')
            + '. The reason is what makes the rejection useful — without it the ledger '
            + 'knows something was wrong but not what.',
      fields: [{ name: 'reason', label: 'Reason', type: 'text' }],
      ok: 'Reject',
      danger: true,
    });
    if (!a) return;                       // backed out: nothing is recorded
    reason = a.reason;
  }
  await recordDecision(p, kind, reviewer, reason, null, card);
}

/** §6.6 — the third decision. An unclassified provision's reading was never
 *  produced by the machine; capturing it is the whole point of this action. */
async function classify(p, card) {
  const host = card || askHost();
  const reviewer = await reviewerName(host);
  if (!reviewer) { say('Not recorded — a decision needs a name.', true); return; }

  // Chips, not a free-text box. R1-R15 is a closed set, and the old prompt
  // accepted anything then rejected it afterwards with an error — asking a
  // question whose wrong answers are knowable in advance.
  const cats = [];
  for (let i = 1; i <= 15; i++) cats.push('R' + i);

  const a = await ask(host, {
    text: 'Which category does ' + p.req_id + ' belong to?',
    detail: 'The extractor filed it under ' + p.category + ' but could not resolve what it '
          + 'does, or who it binds — that is why it is unclassified. Confirm or correct it.',
    fields: [{ name: 'cat', label: 'Category', type: 'choice', options: cats, value: p.category }],
    ok: 'Record classification',
  });
  if (!a || !a.cat) return;

  await recordDecision(p, 'classify', reviewer, '', a.cat, card);
}

/* ------------------------------------------------------------------ wiring */

function selectTab(which) {
  ['margin', 'checklist', 'gaps'].forEach((k) => {
    $('tab-' + k).setAttribute('aria-selected', String(k === which));
    $('panel-' + k).hidden = (k !== which);
  });
}

function showFiles() {
  const ul = $('filelist');
  ul.innerHTML = state.files.map((f, i) => (
    '<li>' + (i === 0 ? '' : '<span>annex </span>') + escapeHtml(f.name) +
    ' <span>' + Math.max(1, Math.round(f.size / 1024)) + ' KB</span></li>'
  )).join('');
  $('btn-review-files').disabled = !state.files.length;
}

function wire() {
  $('btn-review-doc').onclick = reviewFromWord;
  $('btn-review-files').onclick = reviewFromFiles;
  $('btn-annotate').onclick = annotate;
  $('btn-unannotate').onclick = removeAnnotations;
  $('btn-sync-replies').onclick = syncReplies;
  $('btn-schedule').onclick = generateSchedule;
  $('chk-show-reviewed').onchange = toggleShowReviewed;
  $('markup-target').onchange = (e) => { state.markupTarget = e.target.value; };
  $('markup-mode').onchange = (e) => { state.markupMode = e.target.value; };

  $('btn-walk-prev').onclick = () => walkTo(state.walkIndex < 0 ? 0 : state.walkIndex - 1);
  $('btn-walk-next').onclick = () => walkTo(state.walkIndex + 1);
  $('btn-walk-confirm-next').onclick = walkConfirmNext;

  $('filter-category').onchange = (e) => { state.filters.category = e.target.value; applyFilters(); };
  $('filter-item').onchange = (e) => { state.filters.item = e.target.value; applyFilters(); };
  $('filter-reviewed').onchange = (e) => { state.filters.reviewed = e.target.value; applyFilters(); };
  $('btn-filter-clear').onclick = clearFilters;

  $('btn-reset').onclick = () => {
    state.review = null;
    state.files = [];
    state.anchorExec = {};
    state.reviewedState = {};
    showFiles();
    $('result').hidden = true;
    $('idle').hidden = false;
    selectTab('margin');
    health();
  };

  ['margin', 'checklist', 'gaps'].forEach((k) => { $('tab-' + k).onclick = () => selectTab(k); });

  $('btn-mark-submission').onclick = reviewSubmission;
  $('btn-mk-rerun').onclick = reviewSubmission;
  $('btn-mk-annotate').onclick = () => markUpSubmission('marked-up');
  $('btn-mk-comments').onclick = () => markUpSubmission('comments');
  $('btn-mk-unannotate').onclick = removeSubmissionMarks;
  $('btn-mk-reset').onclick = () => {
    $('marks').hidden = true;
    $('idle').hidden = false;
    say('Ready.');
  };

  CGATE_MARKS.init({
    api: API,
    busy: busy,
    say: say,
    escapeHtml: escapeHtml,
    documentBytes: documentBytes,
    documentName: documentName,
    // Read at call time, not captured: boot() decides it after Office.onReady,
    // which resolves later than wire().
    inWord: () => state.inWord,
  });


  $('file').onchange = (e) => { state.files = Array.from(e.target.files || []); showFiles(); };
  $('bundle').onchange = (e) => {
    state.bundleFile = (e.target.files || [])[0] || null;
    $('bundle-name').textContent = state.bundleFile ? state.bundleFile.name : '';
  };

  const drop = $('drop');
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', (e) => {
    state.files = Array.from(e.dataTransfer.files || []);
    showFiles();
  });
}

wire();
boot();
