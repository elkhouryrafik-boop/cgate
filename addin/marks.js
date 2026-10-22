/* ============================================================
   Marks — the examiner's pass over the firm's own submission.

   Everything else in this add-in reads the client's tender and reports what it
   requires. This file reads *our* document and marks it the way a rigorous
   examiner marks a paper: what is right, what is wrong, and what needs an
   answer — each one anchored to the passage it concerns, each one carrying the
   reason it was reached.

   Three marks, and the rule that separates them
   ---------------------------------------------
     ✗  correction    a check ran against a source and this passage failed it
     ?  query         a reader needs the author to answer something
     ✓  commendation  a check ran against a source and this passage passed it

   A commendation is EARNED. The host emits one only where a named check
   executed against a named source and that source affirmed the passage;
   silence never produces one. This file must not weaken that: it never invents
   a mark, never upgrades one, and renders the host's `checks_skipped` list as
   prominently as the marks themselves. A green tick that merely meant "nobody
   looked here" would be the most dangerous thing this add-in could draw,
   because a reviewer trusts the margin more than any report.

   Why a commendation adds no underline
   ------------------------------------
   Corrections get a wave, queries get a dotted underline — Word's own idioms
   for "wrong" and "uncertain", legible without colour, which the brand rules
   require of a compliance surface. A commendation gets a content-control
   boundary and a comment and nothing else. Underlining every sound paragraph
   would deface the document to say "this is fine", and the margin already says
   it. A professor ticks in the margin; they do not underline the good
   sentences.

   This markup is destructive to a document you are about to send. "Mark up a
   copy" exists for that, and the pane says so every time.
*/

'use strict';

const CGATE_MARKS = (() => {

  const KINDS = ['correction', 'query', 'commendation'];

  const GLYPH = { correction: '✗', query: '?', commendation: '✓' };
  const LABEL = { correction: 'Correction', query: 'Query', commendation: 'Verified' };

  // Deep red, amber, violet. Colour is the fast channel; the underline below is
  // the one that survives a monochrome print and a colour-blind reader.
  //
  // The commendation is violet and not green on purpose. §0.2 rules green out
  // of this system entirely: green reads as "cleared", and nothing here is
  // cleared — a `✓` means a named check ran against a named source and that
  // source affirmed the passage, which is a statement about evidence, not a
  // verdict. Violet also keeps the three marks apart for a red-green
  // colour-blind reader, where a green tick and a red correction do not.
  const HEX = { correction: '#a4262c', query: '#8a6100', commendation: '#5b3c8e' };

  // Form, not colour. A commendation deliberately has none — see the header.
  const UNDERLINE = { correction: 'Wave', query: 'Dotted', commendation: 'None' };

  const TAG = 'CGATEMARK:';

  let deps = null;
  let payload = null;
  let expanded = {};

  // Whether a model is reachable on this machine. Asked once, so that a missing
  // model produces an absent button rather than a button that fails when pressed.
  let draftReady = false;
  let draftModel = '';

  function init(d) {
    deps = d;
    probeDraft();
  }

  async function probeDraft() {
    try {
      const r = await fetch(deps.api + '/api/draft/health');
      if (!r.ok) return;
      const j = await r.json();
      draftReady = !!j.ok;
      draftModel = j.ok ? j.detail : '';
    } catch (e) { /* no model, no button. Never an error the reviewer must read. */ }
  }
  function current() { return payload; }

  const esc = (s) => (deps && deps.escapeHtml ? deps.escapeHtml(s) : String(s == null ? '' : s));

  // ---------------------------------------------------------------- host ----

  /** Send the open document to the host and get its marks back.
   *  `opts` carries the optional sources, each of which switches one check on:
   *  { register: File, material: File[], parties: string[], solicitationDocId } */
  async function runReview(opts) {
    opts = opts || {};
    const form = new FormData();

    const bytes = await deps.documentBytes();
    const name = deps.documentName() || 'submission.docx';
    form.append('files', new Blob([bytes]), name);
    form.append('name', name);

    (opts.material || []).forEach((f) => form.append('files', f, f.name));
    if (opts.register) form.append('staff_register', opts.register, opts.register.name);
    if (opts.parties && opts.parties.length) {
      form.append('parties', JSON.stringify(opts.parties));
    }
    if (opts.solicitationDocId) {
      form.append('solicitation_doc_id', opts.solicitationDocId);
    }

    const r = await fetch(deps.api + '/api/submission', { method: 'POST', body: form });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* keep statusText */ }
      throw new Error(detail);
    }
    payload = await r.json();
    expanded = {};
    return payload;
  }

  // ---------------------------------------------------------------- pane ----

  function countsStrip(p) {
    const c = p.counts || {};
    return KINDS.map((k) => (
      '<div class="mk-count mk-' + k + '" data-kind="' + k + '">' +
        '<div class="mk-count-n">' + (c[k] || 0) + '</div>' +
        '<div class="mk-count-l">' + GLYPH[k] + ' ' + LABEL[k].toLowerCase() + '</div>' +
      '</div>'
    )).join('');
  }

  /** The honesty row. A grade sheet reporting zero faults in a dimension
   *  nobody examined reads as a pass, so this is never collapsed away and
   *  never rendered smaller than the counts above it. */
  function skippedRow(p) {
    const r = p.rubric || {};
    const skipped = r.checks_skipped || [];
    const ran = (r.checks_run || []).length;
    const total = ran + skipped.length;
    if (!skipped.length) {
      return '<div class="mk-checks mk-checks-ok">' +
        'All ' + total + ' checks ran.</div>';
    }
    const items = skipped.map((s) => (
      '<li><b>' + esc(s.rule) + '</b> — needs ' + esc(s.needs) +
      '<span class="mk-conseq">' + esc(s.consequence) + '</span></li>'
    )).join('');
    return '<div class="mk-checks mk-checks-warn">' +
      '<div class="mk-checks-head">' + skipped.length + ' of ' + total +
      ' checks did not run. A mark never made is not a passage found sound.</div>' +
      '<ul class="mk-skiplist">' + items + '</ul></div>';
  }

  function markRow(m) {
    const where = m.locator || (m.page ? 'p.' + m.page : '—');
    const open = expanded[m.mark_id];
    const items = (m.checklist_items || []).length
      ? '<div class="mk-items">Checklist ' + m.checklist_items.join(', ') + '</div>' : '';
    const quote = m.quote
      ? '<blockquote class="mk-quote">' + esc(m.quote.slice(0, 320)) + '</blockquote>' : '';
    // A fix is offered only where there is a passage to replace and something
    // wrong with it. A commendation has nothing to fix; a mark with no quote —
    // a shared document author, say — belongs to the file rather than to any
    // sentence in it, and there is nothing to put a suggestion on.
    const canDraft = draftReady && m.kind !== 'commendation' && !!(m.quote || '').trim();
    const draftBlock = canDraft
      ? '<div class="mk-draft" data-draft-for="' + esc(m.mark_id) + '">' +
          '<button class="btn btn-secondary btn-sm" data-act="draft" data-mark="' +
            esc(m.mark_id) + '">Suggest a fix</button>' +
        '</div>'
      : '';

    const body = open
      ? '<div class="mk-body">' +
          '<div class="mk-detail">' + esc(m.detail) + '</div>' + quote +
          '<div class="mk-evidence"><b>How this was reached.</b> ' + esc(m.evidence) + '</div>' +
          items + draftBlock +
        '</div>'
      : '';
    return '<div class="mk-row mk-' + m.kind + (open ? ' is-open' : '') +
             '" data-mark="' + esc(m.mark_id) + '">' +
             '<button class="mk-head" data-act="toggle" data-mark="' + esc(m.mark_id) + '">' +
               '<span class="mk-glyph">' + GLYPH[m.kind] + '</span>' +
               '<span class="mk-heading">' + esc(m.heading) + '</span>' +
               '<span class="mk-where">' + where + '</span>' +
             '</button>' + body +
           '</div>';
  }

  function render(host) {
    if (!payload) { host.innerHTML = ''; return; }
    const r = payload.rubric || {};

    const groups = KINDS.map((kind) => {
      const marks = (payload.marks || []).filter((m) => m.kind === kind);
      if (!marks.length) return '';
      return '<div class="mk-group">' +
        '<div class="mk-group-head">' + GLYPH[kind] + ' ' + LABEL[kind] +
        ' <span class="mk-group-n">' + marks.length + '</span></div>' +
        marks.map(markRow).join('') + '</div>';
    }).join('');

    // Omissions have no passage to mark, so they live here and never in the
    // document. Anchoring one would be a fabrication.
    const missing = (r.mandatory_unaddressed || []);
    const missingBlock = missing.length
      ? '<div class="mk-group">' +
          '<div class="mk-group-head">Required, not found ' +
          '<span class="mk-group-n">' + missing.length + '</span></div>' +
          '<div class="mk-note">An omission has no passage to underline. These are ' +
          'listed here rather than marked in the document.</div>' +
          missing.map((e) => (
            '<div class="mk-row mk-missing"><div class="mk-head-static">' +
              '<span class="mk-glyph">·</span>' +
              '<span class="mk-heading">' + esc(e.clause ? 'Clause ' + e.clause : 'Requirement') +
              ' — ' + esc((e.text || '').slice(0, 110)) + '</span>' +
              '<span class="mk-where">' + (e.page ? 'p.' + e.page : '—') + '</span>' +
            '</div><div class="mk-why">' + esc(e.why) + '</div></div>'
          )).join('') +
        '</div>'
      : '';

    const coverage = r.mandatory_total
      ? '<div class="mk-coverage">' +
          (r.mandatory_addressed || []).length + ' of ' + r.mandatory_total +
          ' deliverable requirement(s) addressed. <span class="mk-caveat">Addressed ' +
          'means the submission uses the requirement’s own terms — not that the ' +
          'answer is sufficient.</span></div>'
      : '';

    host.innerHTML =
      '<div class="mk-counts">' + countsStrip(payload) + '</div>' +
      skippedRow(payload) +
      coverage +
      groups +
      missingBlock;

    host.querySelectorAll('[data-act="toggle"]').forEach((b) => {
      b.addEventListener('click', () => {
        const id = b.getAttribute('data-mark');
        expanded[id] = !expanded[id];
        render(host);
        if (expanded[id]) selectInDocument(id);
      });
    });

    host.querySelectorAll('[data-act="draft"]').forEach((b) => {
      b.addEventListener('click', () => requestDraft(b.getAttribute('data-mark'), host));
    });
  }

  // ------------------------------------------------------------ suggestions ---

  /* A drafted fix is the only thing in this system a language model produces,
   * and the fence around it is worth stating where the code is:
   *
   *   - It proposes WORDS. It never produces a finding, a status or a decision.
   *   - It runs on this machine. `cgate.draft` refuses a non-loopback endpoint
   *     outright, so the answer to "where do the documents go" is unchanged.
   *   - Nothing reaches the document until a named person accepts it, and what
   *     lands is a Word tracked insertion — so Reject is exact removal, drawn by
   *     Word itself rather than by anything invented here.
   *
   * Fixes are offered on our own submission only. `/api/draft` will not look up
   * a mark that is not in a stored submission review, which is what stops this
   * ever proposing an edit to a tender the firm is bidding against.
   */
  async function requestDraft(markId, host) {
    const slot = host.querySelector('[data-draft-for="' + CSS.escape(markId) + '"]');
    if (!slot) return;
    slot.innerHTML = '<div class="mk-drafting">Drafting on this machine…</div>';

    try {
      const r = await fetch(deps.api + '/api/draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ submission_id: payload.submission_id, mark_id: markId }),
      });
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (e) { /* keep statusText */ }
        // 422 means it will never work for this mark — the model declined to
        // invent a source, which is the right answer. 503 means try again.
        slot.innerHTML =
          '<div class="mk-draft-none">' + esc(detail) + '</div>' +
          (r.status === 503
            ? '<button class="btn btn-secondary btn-sm" data-act="draft" data-mark="' +
              esc(markId) + '">Try again</button>'
            : '');
        slot.querySelectorAll('[data-act="draft"]').forEach((b) => {
          b.addEventListener('click', () => requestDraft(markId, host));
        });
        return;
      }
      const d = await r.json();
      renderDraft(slot, markId, d, host);
    } catch (e) {
      slot.innerHTML = '<div class="mk-draft-none">' + esc(e.message) + '</div>';
    }
  }

  function renderDraft(slot, markId, d, host) {
    slot.innerHTML =
      '<div class="mk-suggestion">' +
        '<div class="mk-sug-head">Suggested wording ' +
          '<span class="mk-sug-model">' + esc(d.model) + ', on this machine</span></div>' +
        '<blockquote class="mk-sug-text">' + esc(d.proposed) + '</blockquote>' +
        (d.warnings || []).map((w) =>
          '<div class="mk-sug-warn">' + esc(w) + '</div>').join('') +
        '<div class="mk-sug-note">' + esc(d.disclaimer) + '</div>' +
        '<div class="ask-acts">' +
          '<button class="btn btn-primary btn-sm" data-act="apply">Insert as tracked change</button>' +
          '<button class="btn btn-secondary btn-sm" data-act="discard">Discard</button>' +
        '</div>' +
      '</div>';

    slot.querySelector('[data-act="discard"]').addEventListener('click', () => {
      render(host);
    });
    slot.querySelector('[data-act="apply"]').addEventListener('click', async () => {
      await applyDraft(markId, d, slot);
    });
  }

  /** Put the suggestion into the document as a revision, never as plain text.
   *
   *  Track Changes is turned ON for the insertion and restored afterwards. That
   *  inverts what `checkTrackChanges()` does for markup, and deliberately: a
   *  frame is annotation and must not look like an edit, whereas this IS an
   *  edit and must not look like anything else. Accept and Reject are then
   *  Word's own, which is the only removal guarantee worth having. */
  async function applyDraft(markId, d, slot) {
    if (!deps.inWord || !deps.inWord()) {
      deps.say('Not inserted — this needs the document open in Word.', true);
      return;
    }
    try {
      await Word.run(async (context) => {
        const ccs = context.document.contentControls;
        ccs.load('items/tag');
        await context.sync();

        const hit = ccs.items.find((c) => {
          const p = parseTag(c.tag);
          return p && p.markIds.indexOf(markId) >= 0;
        });
        if (!hit) throw new Error('the passage is not marked in this document — mark it up first');

        context.document.load('changeTrackingMode');
        await context.sync();
        const restore = context.document.changeTrackingMode;

        context.document.changeTrackingMode = Word.ChangeTrackingMode.trackAll;
        await context.sync();

        hit.insertText(d.proposed, Word.InsertLocation.replace);
        await context.sync();

        context.document.changeTrackingMode = restore;
        await context.sync();
      });
      slot.innerHTML =
        '<div class="mk-sug-done">Inserted as a tracked change. Accept or Reject it in ' +
        'Word’s Review tab — nothing is committed until you do.</div>';
    } catch (e) {
      deps.say('Not inserted: ' + e.message, true);
    }
  }

  // ------------------------------------------------------------- document ---

  function parseTag(tag) {
    if (!tag || tag.indexOf(TAG) !== 0) return null;
    const rest = tag.slice(TAG.length);
    const cut = rest.lastIndexOf(':');
    if (cut < 0) return null;
    return { markIds: rest.slice(0, cut).split('+'), kind: rest.slice(cut + 1) };
  }

  function commentBody(m, docId) {
    const L = [];
    L.push('CGATE ' + GLYPH[m.kind] + ' ' + LABEL[m.kind].toUpperCase() +
           ' · ' + (docId ? docId.slice(0, 12) + ' · ' : '') + m.heading);
    L.push('');
    L.push(m.detail);
    L.push('');
    L.push('HOW THIS WAS REACHED: ' + m.evidence);
    if (m.checklist_items && m.checklist_items.length) {
      L.push('');
      L.push('BEARS ON CHECKLIST ITEM(S): ' + m.checklist_items.join(', ') +
             ' of SOP-3.11.');
    }
    L.push('');
    L.push('A mark on our own submission, for the author and the Compliance ' +
           'Officer. Not a determination of the Compliance Officer. ' +
           'Governed by the cgate review-process notes.');
    return L.join('\n');
  }

  function markById() {
    const by = {};
    (payload.marks || []).forEach((m) => { by[m.mark_id] = m; });
    return by;
  }

  /** Anchor every mark that has a quote and frame it. Marks with no quote —
   *  a shared document author belongs to the whole file, not a sentence — are
   *  reported in the pane and deliberately never anchored anywhere. */
  async function markUpContext(context, root, mode) {
    const existing = root.contentControls;
    existing.load('items/tag');
    await context.sync();
    if (existing.items.some((c) => (c.tag || '').indexOf(TAG) === 0)) {
      throw new Error('This document already carries examiner marks. ' +
                      'Remove them first — never nested, never duplicated.');
    }

    deps.busy('Planning anchors …');
    const { plan, executed } = await CGATE_ANCHOR.planAndAnchor(
      context, root, deps.api, payload.submission_id,
      (done, total) => deps.busy('Anchoring … ' + done + '/' + total),
      '/api/submission/anchor-plan'
    );

    const by = markById();
    let placed = 0;
    let idx = 0;

    for (const a of plan.anchors) {
      idx += 1;
      deps.busy('Marking … ' + idx + '/' + plan.anchors.length);
      const primary = executed[a.req_ids[0]];
      if (!primary || !primary.range) continue;

      const kind = a.band;
      const cc = primary.range.insertContentControl();
      cc.tag = TAG + a.req_ids.join('+') + ':' + kind;
      const first = by[a.req_ids[0]];
      cc.title = (GLYPH[kind] || '') + ' ' + (first ? first.heading : LABEL[kind] || '');
      cc.color = HEX[kind] || HEX.query;
      cc.cannotDelete = false;
      cc.appearance = mode === 'comments'
        ? Word.ContentControlAppearance.hidden
        : Word.ContentControlAppearance.boundingBox;

      if (mode === 'marked-up' && UNDERLINE[kind] && UNDERLINE[kind] !== 'None') {
        cc.font.underline = UNDERLINE[kind];
      }

      a.req_ids.forEach((id) => {
        const m = by[id];
        if (m) cc.getRange().insertComment(commentBody(m, payload.submission_id));
      });

      await context.sync();
      placed += a.req_ids.length;
    }

    const anchorable = (payload.marks || []).filter((m) => (m.quote || '').trim()).length;
    return { placed, anchorable, unanchored: anchorable - placed };
  }

  async function markUp(mode) {
    if (!payload) throw new Error('Review the submission first.');
    let outcome = null;
    await Word.run(async (context) => {
      outcome = await markUpContext(context, context.document, mode || 'marked-up');
    });
    return outcome;
  }

  async function removeMarkup() {
    let removed = 0;
    await Word.run(async (context) => {
      const ccs = context.document.contentControls;
      ccs.load('items/tag');
      await context.sync();
      const mine = ccs.items.filter((c) => (c.tag || '').indexOf(TAG) === 0);
      for (const cc of mine) {
        cc.font.underline = 'None';
        cc.delete(true);          // keep the text, drop the control
        removed += 1;
      }
      await context.sync();

      try {
        const comments = context.document.body.getComments();
        comments.load('items/content');
        await context.sync();
        for (const c of comments.items) {
          if ((c.content || '').indexOf('CGATE') === 0) c.delete();
        }
        await context.sync();
      } catch (e) { /* comments API is 1.4+; controls are already gone */ }
    });
    return removed;
  }

  async function selectInDocument(markId) {
    try {
      await Word.run(async (context) => {
        const ccs = context.document.contentControls;
        ccs.load('items/tag');
        await context.sync();
        const hit = ccs.items.find((c) => {
          const p = parseTag(c.tag);
          return p && p.markIds.indexOf(markId) >= 0;
        });
        if (hit) { hit.select(); await context.sync(); }
      });
    } catch (e) { /* selecting is a convenience; never break the pane over it */ }
  }

  return {
    init, runReview, render, markUp, removeMarkup, selectInDocument,
    current, parseTag, commentBody,
    KINDS, GLYPH, LABEL, HEX, UNDERLINE, TAG,
  };
})();
