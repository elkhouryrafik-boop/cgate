"""
cgate.screen — screening named parties against debarment and sanctions sources.

Serves checklist items 3 and 8. It answers one question about one party at a
time: does this name appear on a list that makes it ineligible, and what is the
evidence, retrieved when, from where.

Why this is a client and not a scraper
--------------------------------------
The usual sources — supplier-registry exclusion lists, multilateral-bank listings of
ineligible firms, EU EDES, consolidated sanctions lists —
are published in different formats on different schedules by different bodies.
Aggregating them is a maintained data product, not a feature. OpenSanctions
already publishes them as one deduplicated dataset with an entity-matching API
and an open licence, and a self-hostable server (`yente`) that runs the same
API against a local copy of the data.

So this module is a thin, replaceable client. `ScreeningConfig.base_url` points
at the hosted API, at a yente instance inside the firm's network, or at nothing
at all — in which case `snapshot` reads a downloaded dataset file and matches
locally. That third mode exists because whether cloud services are permitted is
still an open question for this firm, and screening must not be the component
that blocks on the answer.

The rule this module exists to keep
-----------------------------------
**A screening that did not run is not a screening that found nothing.** Every
failure path here returns `unresolved` with a structured, retryable error and
the `source_unreachable` trigger. There is no code path in which an exception
becomes an empty result set, and `evidence()` refuses to emit a clean evidence
bundle when any subject failed to screen.

Secrets
-------
The API key is read from the environment (`OPENSANCTIONS_API_KEY`). It is never
written to a config file, never printed, and never recorded in the evidence —
`source` records the endpoint, not the credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

# Scores are OpenSanctions' own 0..1 entity-match scores.
#
# Tuned for recall, consistent with the design principle: a missed debarred
# counterparty is a serious event, a false flag costs the Compliance
# Officer a few minutes of reading. Anything at or above PARTIAL is shown to a
# human; nothing is ever discarded as "probably a different company".
MATCH_THRESHOLD = 0.80
PARTIAL_THRESHOLD = 0.55

DEFAULT_BASE_URL = "https://api.opensanctions.org"
DEFAULT_DATASET = "default"
DEFAULT_TIMEOUT = 30

# Schema names in the Follow the Money model OpenSanctions uses.
SCHEMA_FOR_KIND = {"firm": "Company", "individual": "Person", "authority": "Organization"}


class ScreeningConfigError(RuntimeError):
    """The screener was asked to run without a usable source."""


# ---------------------------------------------------------------------------


@dataclass
class ScreeningConfig:
    base_url: str = DEFAULT_BASE_URL
    dataset: str = DEFAULT_DATASET
    api_key: str | None = None
    snapshot: str | None = None       # local dataset file; offline mode
    timeout: int = DEFAULT_TIMEOUT
    match_threshold: float = MATCH_THRESHOLD
    partial_threshold: float = PARTIAL_THRESHOLD

    @classmethod
    def from_env(cls, **overrides) -> "ScreeningConfig":
        cfg = cls(
            base_url=os.environ.get("CGATE_SCREENING_URL", DEFAULT_BASE_URL),
            dataset=os.environ.get("CGATE_SCREENING_DATASET", DEFAULT_DATASET),
            api_key=os.environ.get("OPENSANCTIONS_API_KEY") or None,
            snapshot=os.environ.get("CGATE_SCREENING_SNAPSHOT") or None,
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg

    @property
    def source(self) -> str:
        """What is recorded as the evidence source. Never carries the key."""
        if self.snapshot:
            return f"snapshot:{os.path.basename(self.snapshot)}"
        return f"{self.base_url.rstrip('/')}/match/{self.dataset}"


@dataclass
class ScreeningHit:
    entity_id: str
    caption: str
    schema: str
    score: float
    datasets: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    matched_name: str = ""

    @property
    def strength(self) -> str:
        return "match" if self.score >= MATCH_THRESHOLD else "partial"


@dataclass
class ScreeningResult:
    """One subject, screened once, with everything needed to reproduce it."""

    subject: str
    names_queried: list[str]
    source: str
    dataset: str
    queried_at: str
    hits: list[dict] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    response_hash: str | None = None
    entity_id: str | None = None

    @property
    def ok(self) -> bool:
        """True when the screening actually ran. Not a statement about the result."""
        return not self.errors

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _post(url: str, body: dict, api_key: str | None, timeout: int) -> tuple[dict | None, dict | None]:
    """POST JSON. Returns (payload, error). Exactly one is None."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "cgate-screening/1.0")
    if api_key:
        req.add_header("Authorization", f"ApiKey {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        # 401/403 are configuration faults and will not fix themselves on retry.
        # 429 and 5xx will.
        retryable = exc.code in (408, 429) or exc.code >= 500
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:                                     # noqa: BLE001
            pass
        return None, {
            "error_type": f"http_{exc.code}",
            "message": f"{exc.reason}{': ' + detail if detail else ''}",
            "retryable": retryable,
        }
    except urllib.error.URLError as exc:
        return None, {"error_type": "unreachable", "message": str(exc.reason),
                      "retryable": True}
    except (TimeoutError, OSError) as exc:
        return None, {"error_type": "timeout", "message": str(exc), "retryable": True}
    except json.JSONDecodeError as exc:
        return None, {"error_type": "malformed_response", "message": str(exc),
                      "retryable": False}


# ---------------------------------------------------------------------------
# Offline matching
# ---------------------------------------------------------------------------


def _tokens(name: str) -> set[str]:
    from .db import _normalise_name
    return {t for t in _normalise_name(name).split() if len(t) > 1}


def _local_score(query: str, candidate: str) -> float:
    """Token-overlap score for snapshot mode.

    Not a reimplementation of OpenSanctions' matcher and not presented as one.
    It is deliberately blunt and biased toward over-matching, because in offline
    mode the alternative to a crude score is no screening at all, and the output
    goes to a person either way.
    """
    a, b = _tokens(query), _tokens(candidate)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _screen_snapshot(names: list[str], cfg: ScreeningConfig) -> tuple[list[ScreeningHit], str | None, dict | None]:
    """Match against a locally held dataset file.

    Accepts either a JSON array of entities or newline-delimited JSON, which is
    how OpenSanctions publishes its bulk data.
    """
    try:
        with open(cfg.snapshot, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return [], None, {"error_type": "snapshot_unreadable", "message": str(exc),
                          "retryable": True}

    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", "replace").strip()
    try:
        records = json.loads(text) if text.startswith("[") else [
            json.loads(line) for line in text.splitlines() if line.strip()
        ]
    except json.JSONDecodeError as exc:
        return [], digest, {"error_type": "snapshot_malformed", "message": str(exc),
                            "retryable": False}

    hits: list[ScreeningHit] = []
    for rec in records:
        props = rec.get("properties", {}) or {}
        candidates = list(props.get("name") or [])
        if rec.get("caption"):
            candidates.append(rec["caption"])
        best, best_name = 0.0, ""
        for query in names:
            for cand in candidates:
                score = _local_score(query, cand)
                if score > best:
                    best, best_name = score, cand
        if best >= cfg.partial_threshold:
            hits.append(ScreeningHit(
                entity_id=rec.get("id", ""),
                caption=rec.get("caption", best_name),
                schema=rec.get("schema", ""),
                score=round(best, 4),
                datasets=list(rec.get("datasets") or []),
                topics=list(props.get("topics") or []),
                countries=list(props.get("country") or []),
                matched_name=best_name,
            ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits, digest, None


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


def screen_entity(
    name: str,
    *,
    aliases: list[str] | None = None,
    kind: str = "firm",
    country: str | None = None,
    config: ScreeningConfig | None = None,
    entity_id: str | None = None,
) -> ScreeningResult:
    """Screen one party against the configured source.

    Every alias is queried, not just the legal name. A party debarred under a
    trading name it no longer uses is still debarred, and the registry keeps
    alias sets for exactly this call.
    """
    cfg = config or ScreeningConfig.from_env()
    names = [name] + [a for a in (aliases or []) if a and a != name]
    result = ScreeningResult(
        subject=name,
        names_queried=names,
        source=cfg.source,
        dataset=cfg.dataset,
        queried_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        entity_id=entity_id,
    )

    if cfg.snapshot:
        hits, digest, err = _screen_snapshot(names, cfg)
        result.response_hash = digest
        if err:
            result.errors.append({**err, "source": cfg.source})
            result.triggers.append("source_unreachable")
            return result
    else:
        if not cfg.api_key:
            # A missing key is its own configuration fault and must be named as
            # itself. The request is not sent, so no server ever saw these
            # names; "http_401" would say a credential was tried and refused.
            result.errors.append({
                "error_type": "no_api_key",
                "message": "OPENSANCTIONS_API_KEY is not set; the screening "
                           "endpoint was not contacted",
                "retryable": False,
                "source": cfg.source,
            })
            result.triggers.append("source_unreachable")
            return result
        queries = {
            f"q{i}": {
                "schema": SCHEMA_FOR_KIND.get(kind, "LegalEntity"),
                "properties": {
                    "name": [n],
                    **({"country": [country]} if country else {}),
                },
            }
            for i, n in enumerate(names)
        }
        url = f"{cfg.base_url.rstrip('/')}/match/{cfg.dataset}"
        payload, err = _post(url, {"queries": queries}, cfg.api_key, cfg.timeout)
        if err:
            result.errors.append({**err, "source": cfg.source})
            result.triggers.append("source_unreachable")
            return result

        result.response_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        seen: dict[str, ScreeningHit] = {}
        for qid, response in (payload.get("responses") or {}).items():
            idx = int(qid[1:]) if qid[1:].isdigit() else 0
            queried = names[idx] if idx < len(names) else name
            if response.get("status") not in (None, 200):
                result.errors.append({
                    "error_type": f"query_{response.get('status')}",
                    "message": str(response.get("error") or "query rejected"),
                    "retryable": False, "source": cfg.source, "query": queried,
                })
                continue
            for item in response.get("results") or []:
                score = float(item.get("score") or 0.0)
                if score < cfg.partial_threshold:
                    continue
                eid = item.get("id", "")
                props = item.get("properties", {}) or {}
                hit = ScreeningHit(
                    entity_id=eid,
                    caption=item.get("caption", ""),
                    schema=item.get("schema", ""),
                    score=round(score, 4),
                    datasets=list(item.get("datasets") or []),
                    topics=list(props.get("topics") or []),
                    countries=list(props.get("country") or []),
                    matched_name=queried,
                )
                if eid not in seen or hit.score > seen[eid].score:
                    seen[eid] = hit
        hits = sorted(seen.values(), key=lambda h: h.score, reverse=True)

        if result.errors:
            # Some queries ran and some did not. The subject has not been fully
            # screened, and a partial screening reported as a screening is the
            # exact failure this module is built to prevent.
            result.triggers.append("source_unreachable")

    result.hits = [asdict(h) for h in hits]
    if any(h.score >= cfg.match_threshold for h in hits):
        result.triggers.append("any_match")
    if any(cfg.partial_threshold <= h.score < cfg.match_threshold for h in hits):
        result.triggers.append("partial_name_match")
    return result


def screen_many(
    subjects: list[dict],
    config: ScreeningConfig | None = None,
) -> list[ScreeningResult]:
    """Screen a list of {name, aliases, kind, country, entity_id} dicts."""
    cfg = config or ScreeningConfig.from_env()
    return [
        screen_entity(
            s["name"],
            aliases=s.get("aliases"),
            kind=s.get("kind", "firm"),
            country=s.get("country"),
            entity_id=s.get("entity_id"),
            config=cfg,
        )
        for s in subjects
    ]


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def evidence(results: list[ScreeningResult]) -> dict:
    """Shape screening output as an evidence-bundle entry for items 3 and 8.

    The keys are the ones `config/checklist.example.yaml` names for those items, so this drops
    straight into the bundle `cgate review --bundle` reads.

    If any subject failed to screen, `source_unreachable` is carried into the
    entry's triggers, and `adjudicate` turns that into a flag. There is no
    arrangement of inputs that produces a clean entry from an incomplete run.
    """
    triggers: set[str] = set()
    for r in results:
        triggers.update(r.triggers)
    return {
        "screening_result_set": [r.to_dict() for r in results],
        "retrieval_timestamp": max((r.queried_at for r in results), default=""),
        "triggers": sorted(triggers),
    }


def summarise(results: list[ScreeningResult]) -> str:
    """One line per subject, for a terminal."""
    lines = []
    for r in results:
        if r.errors:
            why = r.errors[0].get("error_type", "error")
            retry = " (retryable)" if r.errors[0].get("retryable") else ""
            lines.append(f"  {r.subject}: NOT SCREENED — {why}{retry}")
            continue
        if not r.hits:
            lines.append(f"  {r.subject}: no match in {r.dataset}")
            continue
        top = r.hits[0]
        lines.append(
            f"  {r.subject}: {len(r.hits)} hit(s), top {top['score']:.2f} "
            f"{top['caption']} [{', '.join(top['datasets'][:3]) or 'no dataset'}]"
        )
    return "\n".join(lines)
