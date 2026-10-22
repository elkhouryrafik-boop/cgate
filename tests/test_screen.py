"""
Tests for cgate.screen.

The rule under test throughout: **a screening that did not run is not a
screening that found nothing.** Every failure path must produce a structured
error, the `source_unreachable` trigger, and an evidence entry that cannot be
mistaken for a clean one.
"""

from __future__ import annotations

import json

import pytest

from cgate import screen
from cgate.screen import ScreeningConfig


@pytest.fixture()
def snapshot(tmp_path):
    """A tiny local dataset in the shape OpenSanctions publishes."""
    records = [
        {"id": "ex-001", "caption": "Acme Engineering Ltd", "schema": "Company",
         "datasets": ["example_exclusion"],
         "properties": {"name": ["Acme Engineering Ltd", "Acme Eng Ltd"],
                        "country": ["xx"], "topics": ["debarment"]}},
        {"id": "un-002", "caption": "Wholly Unrelated Trading Co", "schema": "Company",
         "datasets": ["un_sc_sanctions"],
         "properties": {"name": ["Wholly Unrelated Trading Co"]}},
    ]
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return str(path)


def cfg_for(snapshot_path, **kw):
    return ScreeningConfig(snapshot=snapshot_path, dataset="debarment", **kw)


# ---------------------------------------------------------------------------
# Offline matching
# ---------------------------------------------------------------------------


def test_snapshot_finds_a_debarred_firm(snapshot):
    r = screen.screen_entity("Acme Engineering Ltd", config=cfg_for(snapshot))
    assert r.ok
    assert r.hits
    assert r.hits[0]["entity_id"] == "ex-001"
    assert "any_match" in r.triggers
    assert "example_exclusion" in r.hits[0]["datasets"]


def test_snapshot_clean_result_is_distinguishable_from_a_failure(snapshot):
    r = screen.screen_entity("Entirely Different Consultants", config=cfg_for(snapshot))
    assert r.ok
    assert r.hits == []
    assert r.triggers == []
    # A clean screening still records where and when it ran.
    assert r.response_hash and r.queried_at and r.source.startswith("snapshot:")


def test_aliases_are_queried_not_just_the_legal_name(snapshot):
    """A party debarred under a trading name it no longer uses is still debarred."""
    r = screen.screen_entity("Wholly Different Name LLC",
                             aliases=["Acme Engineering Ltd"],
                             config=cfg_for(snapshot))
    assert r.hits and r.hits[0]["entity_id"] == "ex-001"
    assert "Acme Engineering Ltd" in r.names_queried


def test_missing_snapshot_is_an_error_not_an_empty_result(tmp_path):
    r = screen.screen_entity("Anyone", config=cfg_for(str(tmp_path / "nope.json")))
    assert not r.ok
    assert r.hits == []
    assert "source_unreachable" in r.triggers
    assert r.errors[0]["error_type"] == "snapshot_unreadable"
    assert r.errors[0]["retryable"] is True


def test_malformed_snapshot_is_not_retryable(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all", encoding="utf-8")
    r = screen.screen_entity("Anyone", config=cfg_for(str(bad)))
    assert not r.ok
    assert r.errors[0]["retryable"] is False


def test_snapshot_hash_is_recorded_for_reproducibility(snapshot):
    a = screen.screen_entity("Acme Engineering Ltd", config=cfg_for(snapshot))
    b = screen.screen_entity("Acme Engineering Ltd", config=cfg_for(snapshot))
    assert a.response_hash == b.response_hash


# ---------------------------------------------------------------------------
# HTTP path
# ---------------------------------------------------------------------------


def _fake_post(payload=None, error=None):
    def _post(url, body, api_key, timeout):
        return payload, error
    return _post


def test_http_match_produces_a_hit(monkeypatch):
    monkeypatch.setattr(screen, "_post", _fake_post(payload={
        "responses": {"q0": {"status": 200, "results": [
            {"id": "NK-abc", "caption": "Acme Engineering Ltd",
             "schema": "Company", "score": 0.93,
             "datasets": ["example_exclusion", "eu_edes"],
             "properties": {"topics": ["debarment"], "country": ["xx"]}}]}}}))
    r = screen.screen_entity("Acme Engineering Ltd", config=ScreeningConfig(api_key="k-test"))
    assert r.ok
    assert "any_match" in r.triggers
    assert r.hits[0]["score"] == 0.93


def test_partial_match_is_surfaced_separately(monkeypatch):
    monkeypatch.setattr(screen, "_post", _fake_post(payload={
        "responses": {"q0": {"status": 200, "results": [
            {"id": "NK-x", "caption": "Acme Engineering Group", "schema": "Company",
             "score": 0.63, "datasets": ["example_exclusion"], "properties": {}}]}}}))
    r = screen.screen_entity("Acme Engineering Ltd", config=ScreeningConfig(api_key="k-test"))
    assert "partial_name_match" in r.triggers
    assert "any_match" not in r.triggers


def test_low_scoring_noise_is_dropped(monkeypatch):
    monkeypatch.setattr(screen, "_post", _fake_post(payload={
        "responses": {"q0": {"status": 200, "results": [
            {"id": "NK-y", "caption": "Something Else", "schema": "Company",
             "score": 0.11, "datasets": [], "properties": {}}]}}}))
    r = screen.screen_entity("Acme", config=ScreeningConfig(api_key="k-test"))
    assert r.hits == []
    assert r.triggers == []


def test_unreachable_source_never_reads_as_clean(monkeypatch):
    monkeypatch.setattr(screen, "_post", _fake_post(error={
        "error_type": "unreachable", "message": "connection refused", "retryable": True}))
    r = screen.screen_entity("Acme", config=ScreeningConfig(api_key="k-test"))
    assert not r.ok
    assert r.hits == []
    assert "source_unreachable" in r.triggers


@pytest.mark.parametrize("code,retryable", [(429, True), (503, True), (401, False), (403, False)])
def test_http_error_retryability(monkeypatch, code, retryable):
    import urllib.error

    def boom(url, data=None, timeout=None):
        raise urllib.error.HTTPError(url, code, "err", {}, None)

    monkeypatch.setattr(screen.urllib.request, "urlopen", boom)
    r = screen.screen_entity("Acme", config=ScreeningConfig(api_key="k-test"))
    assert not r.ok
    assert r.errors[0]["retryable"] is retryable


def test_no_api_key_is_named_as_itself_and_sends_nothing(monkeypatch):
    """No key means the request never leaves the machine, and the error says so.

    A missing key must not surface as http_401, which would say a credential
    was tried and refused; the names were never sent to any server.
    """
    def must_not_be_called(url, body, api_key, timeout):
        raise AssertionError("screening request sent without an API key")

    monkeypatch.setattr(screen, "_post", must_not_be_called)
    r = screen.screen_entity("Acme", config=ScreeningConfig())
    assert not r.ok
    assert r.errors[0]["error_type"] == "no_api_key"
    assert r.errors[0]["retryable"] is False
    assert "source_unreachable" in r.triggers


def test_a_partially_failed_run_is_flagged_even_though_some_queries_matched(monkeypatch):
    """One alias screened, another rejected. The subject is not fully screened."""
    monkeypatch.setattr(screen, "_post", _fake_post(payload={
        "responses": {
            "q0": {"status": 200, "results": [
                {"id": "NK-1", "caption": "Acme", "schema": "Company", "score": 0.91,
                 "datasets": ["example_exclusion"], "properties": {}}]},
            "q1": {"status": 500, "error": "backend unavailable"},
        }}))
    r = screen.screen_entity("Acme", aliases=["Acme Trading"], config=ScreeningConfig(api_key="k-test"))
    assert r.hits
    assert not r.ok
    assert "source_unreachable" in r.triggers


def test_api_key_never_appears_in_the_evidence():
    cfg = ScreeningConfig(api_key="secret-key-do-not-log")
    assert "secret" not in cfg.source
    r = screen.ScreeningResult(subject="x", names_queried=["x"], source=cfg.source,
                               dataset="default", queried_at="now")
    assert "secret" not in json.dumps(r.to_dict())


def test_config_reads_key_from_env_not_from_a_file(monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "k-123")
    monkeypatch.setenv("CGATE_SCREENING_URL", "https://yente.internal")
    cfg = ScreeningConfig.from_env()
    assert cfg.api_key == "k-123"
    assert cfg.base_url == "https://yente.internal"


# ---------------------------------------------------------------------------
# Evidence bundle
# ---------------------------------------------------------------------------


def test_evidence_uses_the_keys_the_checklist_names(snapshot):
    results = screen.screen_many(
        [{"name": "Acme Engineering Ltd"}, {"name": "Clean Consultants"}],
        config=cfg_for(snapshot))
    ev = screen.evidence(results)
    assert set(ev) == {"screening_result_set", "retrieval_timestamp", "triggers"}
    assert len(ev["screening_result_set"]) == 2
    assert ev["retrieval_timestamp"]


def test_evidence_carries_failure_into_the_bundle(tmp_path, snapshot):
    """One subject screened clean, one never screened. The entry must not look clean."""
    results = [
        screen.screen_entity("Clean Consultants", config=cfg_for(snapshot)),
        screen.screen_entity("Never Screened", config=cfg_for(str(tmp_path / "gone.json"))),
    ]
    ev = screen.evidence(results)
    assert "source_unreachable" in ev["triggers"]


def test_evidence_flows_through_adjudication_as_a_flag(snapshot, tmp_path):
    """The bundle entry produced here must make adjudicate() flag items 3 and 8."""
    from cgate.adjudicate import adjudicate, load_checklist
    from cgate.model import Document, Status

    checklist = load_checklist("config/checklist.example.yaml")
    doc = Document(doc_id="d" * 64, path="x.pdf", filetype="pdf", n_pages=1)

    failed = screen.screen_entity("Never Screened",
                                  config=cfg_for(str(tmp_path / "gone.json")))
    bundle = {"3": screen.evidence([failed])}
    items = {i.item_id: i for i in adjudicate(checklist, doc, [], bundle)}
    assert items[3].status is Status.FLAG
    assert "source_unreachable" in items[3].triggers


def test_a_hit_on_the_firm_itself_flags_item_3(snapshot):
    """A hit on the bidding firm must always flag."""
    from cgate.adjudicate import adjudicate, load_checklist
    from cgate.model import Document, Status

    checklist = load_checklist("config/checklist.example.yaml")
    doc = Document(doc_id="d" * 64, path="x.pdf", filetype="pdf", n_pages=1)
    hit = screen.screen_entity("Acme Engineering Ltd", config=cfg_for(snapshot))
    items = {i.item_id: i for i in adjudicate(checklist, doc, [], {"3": screen.evidence([hit])})}
    assert items[3].status is Status.FLAG
    assert "any_match" in items[3].triggers
