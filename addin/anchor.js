/* ============================================================
   Anchor execution — the anchor plan (execution side)

   The host (cgate/anchor.py, POST /api/anchor-plan) does the hard part: it
   aligns each provision's quote against the document's own body text and
   decides what to search for and which hit is ours. Everything in this file
   is the part that genuinely needs Word:

     1. Ask Word for the body text and send it to the host.
     2. Execute the plan it returns — search for the planned fragment, take
        the planned ordinal hit.
     3. Verify before anchoring. A range whose text does not match what the
        plan expected is not anchored at a lower confidence — it is the wrong
        range, and is reported `failed` rather than framed.
     4. For two-point (multi-paragraph) anchors, expandTo the span between
        head and tail, feature-detecting WordApi 1.3 and degrading to a
        head-only anchor with a stated reason when it is unavailable.

   This module never decides what a provision's band is or whether it should
   be anchored at all — the plan already decided that. It only executes.
*/

'use strict';

const CGATE_ANCHOR = (() => {

  /** Same whitespace-collapse comparison the plan and the runtime text
   *  should agree on. Case-insensitive, matching Word's default search. */
  function normForCompare(s) {
    return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  }

  /** `endpoint` lets a caller plan against a different stored review.
   *  A provision's quote and an examiner's mark are the same problem — text
   *  taken from one representation of a document, searched for in another —
   *  so the submission marks reuse this module unchanged and only point it at
   *  /api/submission/anchor-plan instead. */
  async function fetchPlan(apiOrigin, docId, bodyText, endpoint) {
    const r = await fetch(apiOrigin + (endpoint || '/api/anchor-plan'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc_id: docId, body_text: bodyText }),
    });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* keep statusText */ }
      throw new Error('anchor plan: ' + detail);
    }
    return r.json();
  }

  /** Find the plan's ordinal-th hit of one fragment and verify its text
   *  before trusting it. §4.2.1-2. `root` is whatever exposes `.body` for the
   *  target document — `context.document` when marking up the open document,
   *  or the not-yet-opened `Document` proxy from `Application.createDocument`
   *  when marking up a copy (§5.1) — both are valid within the same context,
   *  and this module never assumes which one it was handed. */
  async function locateFragment(context, root, frag) {
    if (!frag) return { range: null, reason: 'no fragment in the plan' };
    const results = root.body.search(
      frag.text, { matchCase: false, matchWholeWord: false }
    );
    results.load('items/text');
    await context.sync();

    const hit = results.items[frag.ordinal - 1];
    if (!hit) {
      return {
        range: null,
        reason: 'the planned occurrence #' + frag.ordinal + ' of "' +
          frag.text.slice(0, 60) + (frag.text.length > 60 ? '…' : '') +
          '" was not found in the open document — it may have changed since the plan was made',
      };
    }
    if (normForCompare(hit.text) !== normForCompare(frag.text)) {
      return {
        range: null,
        reason: 'text at the planned location does not match what the plan expected — ' +
          'not anchoring a range that might be the wrong one',
      };
    }
    return { range: hit, reason: null };
  }

  /** Execute one provision's plan entry. Returns
   *  { range, result, reason } — `range` is a Word.Range or null. `result`
   *  can be downgraded from the plan's own result (e.g. exact -> fragment) if
   *  execution could only partly honour it. */
  async function executeEntry(context, root, entry, canExpand) {
    if (!entry || (entry.result !== 'exact' && entry.result !== 'fragment')) {
      return { range: null, result: entry ? entry.result : 'failed', reason: entry ? entry.reason : 'no plan entry' };
    }

    const head = await locateFragment(context, root, entry.head);
    if (!head.range) {
      return { range: null, result: 'failed', reason: head.reason };
    }
    if (!entry.tail) {
      return { range: head.range, result: entry.result, reason: entry.reason };
    }

    const tail = await locateFragment(context, root, entry.tail);
    if (!tail.range) {
      return {
        range: head.range, result: 'fragment',
        reason: 'the head of a two-point anchor was found but the tail could not be verified — ' + tail.reason,
      };
    }
    if (!canExpand) {
      return {
        range: head.range, result: 'fragment',
        reason: 'a two-point (multi-paragraph) anchor was planned but Range.expandTo ' +
          '(WordApi 1.3) is not available on this host — anchored to the first passage only',
      };
    }

    const span = head.range.expandTo(tail.range);
    span.load('text');
    await context.sync();
    return { range: span, result: entry.result, reason: entry.reason };
  }

  /** Plan and execute anchors for every provision in the current review.
   *  `root` — whatever exposes `.body` for the target document (see
   *  `locateFragment`). `apiOrigin` — where cgate serve is listening.
   *  `docId` — the review's document id. Returns { plan, executed } where
   *  `executed` is keyed by req_id:
   *  { range, result, reason, anchorId, band, coalescedWith }. */
  async function planAndAnchor(context, root, apiOrigin, docId, onProgress, endpoint) {
    root.body.load('text');
    await context.sync();
    const bodyText = root.body.text;

    const plan = await fetchPlan(apiOrigin, docId, bodyText, endpoint);
    let canExpand = true;
    try {
      canExpand = Office.context.requirements.isSetSupported('WordApi', '1.3');
    } catch (e) {
      canExpand = false;
    }

    const executed = {};
    let done = 0;
    const total = plan.anchors.length;
    for (const a of plan.anchors) {
      done += 1;
      if (onProgress) onProgress(done, total);
      const primaryEntry = plan.results[a.req_ids[0]];
      const outcome = await executeEntry(context, root, primaryEntry, canExpand);
      a.req_ids.forEach((reqId) => {
        executed[reqId] = {
          range: outcome.range,
          result: outcome.range ? outcome.result : outcome.result,
          reason: outcome.reason,
          anchorId: outcome.range ? a.anchor_id : null,
          band: a.band,
          coalescedWith: a.req_ids.filter((r) => r !== reqId),
        };
      });
    }

    // Every provision the plan named gets an outcome, including the ones
    // that were never grouped into an anchor because they were ambiguous or
    // failed at the planning stage — absent is not clean (§9.5).
    Object.keys(plan.results).forEach((reqId) => {
      if (executed[reqId]) return;
      const entry = plan.results[reqId];
      executed[reqId] = {
        range: null, result: entry.result, reason: entry.reason,
        anchorId: null, band: null, coalescedWith: [],
      };
    });

    return { plan, executed };
  }

  return { planAndAnchor, executeEntry, locateFragment, normForCompare };
})();
