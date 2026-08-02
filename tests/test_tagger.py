"""Tests for the run engine, focused on high-water mark advancement.

Getting this wrong is silent: the mark moves past an article that was never
tagged and nothing ever revisits it. So each rule about when the mark may move
gets its own test.
"""

import pytest

from inoreader_tagger.api import InoreaderAuthError, InoreaderError
from inoreader_tagger.db import (
    STATUS_AUTH_REQUIRED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
)
from inoreader_tagger.tagger import RunParameters, TaggerEngine

RULES = [{"pattern": "example.com", "match_type": "domain", "tags": ["Example"]}]


def article(article_id: str, timestamp: int, url: str = "https://example.com/a", categories=None):
    return {
        "id": article_id,
        "timestampUsec": str(timestamp),
        "canonical": [{"href": url}],
        "categories": categories or [],
        "title": article_id,
    }


class FakeAPI:
    """Stands in for InoreaderAPI, with scripted batches and failures."""

    def __init__(self, batches, tag_failures=(), refresh_error=None):
        self._batches = list(batches)
        self._tag_failures = set(tag_failures)
        self._refresh_error = refresh_error
        self.refresh_token = "stored-token"
        self.tag_calls = []

    def refresh_access_token(self):
        if self._refresh_error:
            raise self._refresh_error
        return {"access_token": "fresh"}

    def get_unread_articles(self, count, folder_name=None, since_timestamp=None):
        if not self._batches:
            return []
        return self._batches.pop(0)

    def add_tag_to_articles_batch(self, article_ids, tag_name):
        self.tag_calls.append((tag_name, list(article_ids)))
        if tag_name in self._tag_failures:
            return False, "HTTP 500: boom"
        return True, ""


def run_engine(api, **kwargs):
    params = RunParameters(rules=RULES, **kwargs)
    return TaggerEngine(api, params).run()


def test_successful_run_advances_the_high_water_mark():
    api = FakeAPI([[article("a", 100), article("b", 300)]])
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_SUCCESS
    assert outcome.tagged == 2
    assert outcome.new_timestamp == "300"


def test_failed_tagging_keeps_the_mark_and_reports_partial():
    api = FakeAPI([[article("a", 100)]], tag_failures={"Example"})
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_PARTIAL
    assert outcome.errors == 1
    # The one article failed, so there is no clean article to advance to.
    assert outcome.new_timestamp is None


def test_mark_advances_only_to_the_newest_cleanly_processed_article():
    # 'b' fails its tag; 'a' succeeds with a different tag. The mark must stop
    # at 'a' so 'b' is retried next run.
    rules = [
        {"pattern": "good.com", "match_type": "domain", "tags": ["Good"]},
        {"pattern": "bad.com", "match_type": "domain", "tags": ["Bad"]},
    ]
    api = FakeAPI(
        [[
            article("a", 100, url="https://good.com/x"),
            article("b", 500, url="https://bad.com/y"),
        ]],
        tag_failures={"Bad"},
    )
    outcome = TaggerEngine(api, RunParameters(rules=rules, max_articles=10, batch_size=10)).run()

    assert outcome.errors == 1
    assert outcome.new_timestamp == "100"


def test_hitting_the_article_ceiling_does_not_advance_the_mark():
    # Exactly max_articles came back, so older unread articles may remain.
    api = FakeAPI([[article("a", 100), article("b", 200)]])
    outcome = run_engine(api, max_articles=2, batch_size=2)

    assert outcome.status == STATUS_SUCCESS
    assert outcome.new_timestamp is None


def test_force_flag_advances_despite_the_ceiling():
    api = FakeAPI([[article("a", 100), article("b", 200)]])
    outcome = run_engine(api, max_articles=2, batch_size=2, force_timestamp_update=True)

    assert outcome.new_timestamp == "200"


def test_dry_run_tags_nothing_and_never_advances_the_mark():
    api = FakeAPI([[article("a", 100)]])
    outcome = run_engine(api, max_articles=10, batch_size=10, dry_run=True)

    assert outcome.new_timestamp is None
    assert api.tag_calls == []


def test_mark_never_moves_backwards():
    api = FakeAPI([[article("a", 100)]])
    outcome = run_engine(api, max_articles=10, batch_size=10, since_timestamp="500")

    assert outcome.new_timestamp is None


def test_articles_already_carrying_the_tag_are_skipped_not_retagged():
    api = FakeAPI([[article("a", 100, categories=["user/-/label/Example"])]])
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.skipped == 1
    assert api.tag_calls == []
    # Nothing failed, so this article may still advance the mark.
    assert outcome.new_timestamp == "100"


def test_articles_without_a_url_are_skipped_cleanly():
    api = FakeAPI([[{"id": "a", "timestampUsec": "100", "categories": []}]])
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.skipped == 1
    assert outcome.new_timestamp == "100"


def test_expired_refresh_token_is_reported_as_auth_required():
    api = FakeAPI([], refresh_error=InoreaderAuthError("token dead"))
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_AUTH_REQUIRED
    assert "token dead" in outcome.error_message


def test_transient_refresh_failure_is_not_treated_as_auth_required():
    api = FakeAPI([], refresh_error=InoreaderError("HTTP 503"))
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_FAILED


def test_no_rules_is_a_no_op_success():
    api = FakeAPI([[article("a", 100)]])
    outcome = TaggerEngine(api, RunParameters(rules=[])).run()

    assert outcome.status == STATUS_SUCCESS
    assert outcome.new_timestamp is None


def test_pagination_stops_when_a_short_batch_comes_back():
    api = FakeAPI([
        [article(f"a{i}", 100 + i) for i in range(5)],
        [article("tail", 900)],
    ])
    outcome = run_engine(api, max_articles=20, batch_size=5)

    assert outcome.processed == 6
    assert outcome.new_timestamp == "900"
