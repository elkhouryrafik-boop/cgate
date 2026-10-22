-- Compliance Review Register and audit ledger
-- Implements the record-keeping requirement of SOP-3.10 §10.
--
-- Two principles are enforced here rather than left to convention:
--
--   1. APPEND-ONLY. Nothing is updated or deleted. State changes are new rows.
--      Triggers below reject UPDATE and DELETE on the ledger outright.
--      Retention: 7 years minimum (config: retention_years).
--
--   2. NO MACHINE DECISIONS. Any row that records a clearance, a conditional
--      clearance, a hold, the lifting of a hold, or the closing of a register
--      entry MUST carry a human actor and a signature reference. The CHECK
--      constraint on `decision` makes a system-authored decision impossible
--      to insert.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Entities: firms, individuals, and the relationships between them.
-- This is the substrate that makes cross-pursuit conflict detection possible.
-- ---------------------------------------------------------------------------

CREATE TABLE entity (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('firm','individual','authority')),
    legal_name      TEXT NOT NULL,
    aliases         TEXT,                      -- JSON array; screening needs these
    jurisdiction    TEXT,
    registration_no TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT NOT NULL
);

CREATE TABLE relationship (
    id          INTEGER PRIMARY KEY,
    from_entity TEXT NOT NULL REFERENCES entity(id),
    to_entity   TEXT NOT NULL REFERENCES entity(id),
    kind        TEXT NOT NULL,                 -- subcontractor_of, employee_of, agent_of, ...
    valid_from  TEXT,
    valid_to    TEXT,
    source      TEXT NOT NULL,                 -- where this was established
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Pursuits: one row per opportunity within SOP-3.10 §2 scope.
-- Created at NOTIFICATION, which 3.10 §4.1 requires BEFORE any internal
-- decision to pursue and before resources are committed.
-- ---------------------------------------------------------------------------

CREATE TABLE pursuit (
    id                  TEXT PRIMARY KEY,
    client_entity       TEXT NOT NULL REFERENCES entity(id),
    scope_category      TEXT NOT NULL CHECK (scope_category IN ('A','B')),  -- 3.10 §2
    tender_reference    TEXT,
    title               TEXT NOT NULL,
    jurisdiction        TEXT,                   -- ISO 3166-1 alpha-2 place of performance,
                                                -- which is not always the client's own
                                                -- country. Checklist item 5 reads this.
    estimated_value     REAL,
    currency            TEXT,
    submission_deadline TEXT NOT NULL,
    notified_at         TEXT NOT NULL,          -- clock start for 3.10 §4.3
    notified_by         TEXT NOT NULL,          -- BD Manager
    review_track        TEXT NOT NULL DEFAULT 'standard'
                        CHECK (review_track IN ('standard','fast_track')),
    fast_track_approved_by TEXT,                -- ECC Chair; required if fast_track
    fast_track_approved_at TEXT,
    CHECK (review_track = 'standard'
           OR (fast_track_approved_by IS NOT NULL AND fast_track_approved_at IS NOT NULL))
);

-- Parties named in a submission. Each needs a signed declaration (item 6/7/9).
CREATE TABLE pursuit_party (
    pursuit_id          TEXT NOT NULL REFERENCES pursuit(id),
    entity_id           TEXT NOT NULL REFERENCES entity(id),
    role                TEXT NOT NULL,          -- subcontractor, consortium_partner, agent
    declaration_ref     TEXT,                   -- signed counterparty declaration
    declaration_signed_at TEXT,
    PRIMARY KEY (pursuit_id, entity_id, role)
);

-- Pre-bid engagement: site visits, pre-bid meetings, clarification rounds.
-- Checklist item 6 mechanism 3. Attendance is recorded because a firm and its
-- subcontractor attending the same pre-bid site visit can indicate coordinated
-- bidding; an attendance list nobody kept is not evidence of anything.
CREATE TABLE prebid_engagement (
    id          INTEGER PRIMARY KEY,
    pursuit_id  TEXT NOT NULL REFERENCES pursuit(id),
    kind        TEXT NOT NULL CHECK (kind IN
                    ('site_visit','prebid_meeting','clarification','other')),
    occurred_at TEXT NOT NULL,
    location    TEXT,
    -- Entities observed present. A party observed here that is not named on the
    -- pursuit, or a known competitor, is what the cross-check looks for.
    attendee_entity TEXT REFERENCES entity(id),
    attendee_name   TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    source      TEXT,                           -- sign-in sheet, minutes, photograph
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_prebid_pursuit ON prebid_engagement(pursuit_id);

-- ---------------------------------------------------------------------------
-- Checklist evidence: one row per item per pursuit, per evaluation.
-- `status` is what the SYSTEM found. It is never a decision.
-- ---------------------------------------------------------------------------

CREATE TABLE check_result (
    id           INTEGER PRIMARY KEY,
    pursuit_id   TEXT NOT NULL REFERENCES pursuit(id),
    item_id      TEXT NOT NULL,                 -- config/checklist.example.yaml item id
    -- Vocabulary is model.Status, exactly. There is no 'verified', no
    -- 'cleared' and no 'satisfied' here for the same reason there is none in
    -- the enum: the strongest thing the system may record is that the evidence
    -- asked for is on file. Clearance is a row in `decision`, and needs a human.
    status       TEXT NOT NULL CHECK (status IN
                     ('evidence_present','flag','requires_human',
                      'unresolved','not_applicable')),
    evidence     TEXT NOT NULL,                 -- JSON: sources, timestamps, hashes
    triggers     TEXT,                          -- JSON array of structural triggers
    source_error TEXT,                          -- JSON if a source was unreachable
    evaluated_at TEXT NOT NULL DEFAULT (datetime('now')),
    tool_version TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Decisions. Human-only, by construction.
-- ---------------------------------------------------------------------------

CREATE TABLE decision (
    id             INTEGER PRIMARY KEY,
    pursuit_id     TEXT NOT NULL REFERENCES pursuit(id),
    decision       TEXT NOT NULL CHECK (decision IN
                       ('clearance','conditional_clearance','information_requested',
                        'hold','hold_lifted','entry_closed')),
    conditions     TEXT,
    reasons        TEXT NOT NULL,               -- 3.10 §6.3 requires stated reasons
    actor_name     TEXT NOT NULL,               -- a named natural person
    actor_role     TEXT NOT NULL CHECK (actor_role IN ('CO','ECC','Board')),
    signature_ref  TEXT NOT NULL,               -- pointer to the executed record
    decided_at     TEXT NOT NULL,
    -- A decision with no named human and no signature cannot be recorded.
    CHECK (length(trim(actor_name)) > 0 AND length(trim(signature_ref)) > 0)
);

-- ---------------------------------------------------------------------------
-- Audit ledger. Append-only. This is what makes the register defensible.
-- ---------------------------------------------------------------------------

CREATE TABLE ledger (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL DEFAULT (datetime('now')),
    pursuit_id     TEXT,
    event          TEXT NOT NULL,
    actor          TEXT NOT NULL,               -- person name, or 'system:<component>'
    payload        TEXT NOT NULL,               -- JSON
    checklist_ver  TEXT NOT NULL,
    tool_version   TEXT NOT NULL,
    prev_hash      TEXT,                        -- chain: sha256 of previous row
    row_hash       TEXT NOT NULL
);

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TRIGGER decision_no_update BEFORE UPDATE ON decision
BEGIN SELECT RAISE(ABORT, 'decisions are immutable; record a new decision'); END;

CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;

-- ---------------------------------------------------------------------------
-- The register view required by SOP-3.10 §10, reviewable by the ECC
-- at each quarterly meeting and available to auditors and international
-- organisations on request.
-- ---------------------------------------------------------------------------

CREATE VIEW compliance_review_register AS
SELECT
    p.id                        AS pursuit_id,
    p.title                     AS submission,
    e.legal_name                AS client,
    p.tender_reference,
    p.notified_at               AS notification_received,
    p.review_track,
    p.submission_deadline,
    d.decision                  AS outcome,
    d.decided_at                AS outcome_date,
    d.conditions,
    d.actor_name                AS decided_by,
    d.actor_role,
    d.signature_ref
FROM pursuit p
JOIN entity e ON e.id = p.client_entity
LEFT JOIN decision d ON d.pursuit_id = p.id
ORDER BY p.notified_at DESC;

CREATE INDEX idx_check_pursuit ON check_result(pursuit_id, item_id);
CREATE INDEX idx_ledger_pursuit ON ledger(pursuit_id);
CREATE INDEX idx_party_entity ON pursuit_party(entity_id);
