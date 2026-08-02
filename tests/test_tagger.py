"""Tests for the run engine, focused on high-water mark advancement.

Getting this wrong is silent: the mark moves past an article that was never
tagged and nothing ever revisits it. So each rule about when the mark may move
gets its own test.

Articles are fetched oldest-first, which is what makes the mark a resumable
cursor — everything below it is genuinely done, so stopping early is safe.
"""

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
    """Stands in for InoreaderAPI, modelling continuation-based paging.

    Pages are returned in order, and every page but the last hands back a
    continuation token — mirroring the real API, where that token is the only
    trustworthy signal that the stream is exhausted.
    """

    def __init__(self, pages, tag_failures=(), refresh_error=None):
        self._pages = list(pages)
        self._tag_failures = set(tag_failures)
        self._refresh_error = refresh_error
        self.refresh_token = "stored-token"
        self.tag_calls = []
        self.fetch_calls = []

    def refresh_access_token(self):
        if self._refresh_error:
            raise self._refresh_error
        return {"access_token": "fresh"}

    def get_unread_articles(self, count, folder_name=None, since_timestamp=None,
                            continuation=None, oldest_first=True):
        self.fetch_calls.append({"count": count, "continuation": continuation})

        index = 0 if continuation is None else int(continuation)
        if index >= len(self._pages):
            return [], None

        page = self._pages[index][:count]
        next_token = str(index + 1) if index + 1 < len(self._pages) else None
        return page, next_token

    def add_tag_to_articles_batch(self, article_ids, tag_name):
        self.tag_calls.append((tag_name, list(article_ids)))
        if tag_name in self._tag_failures:
            return False, "HTTP 500: boom"
        return True, ""


def run_engine(api, **kwargs):
    return TaggerEngine(api, RunParameters(rules=RULES, **kwargs)).run()


def test_successful_run_advances_to_the_newest_article():
    api = FakeAPI([[article("a", 100), article("b", 300)]])
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_SUCCESS
    assert outcome.tagged == 2
    assert outcome.new_timestamp == "300"


def test_pagination_follows_the_continuation_token():
    api = FakeAPI([
        [article("a", 100), article("b", 200)],
        [article("c", 300), article("d", 400)],
    ])
    outcome = run_engine(api, max_articles=10, batch_size=2)

    # Two distinct pages, not the same page fetched twice.
    assert [c["continuation"] for c in api.fetch_calls] == [None, "1"]
    assert outcome.processed == 4
    assert outcome.new_timestamp == "400"


def test_a_full_page_is_not_mistaken_for_the_end_of_the_stream():
    # The first page is exactly batch_size, so a length check would stop here.
    # Only the continuation token reveals there is more.
    api = FakeAPI([
        [article("a", 100), article("b", 200)],
        [article("c", 300)],
    ])
    outcome = run_engine(api, max_articles=10, batch_size=2)

    assert outcome.processed == 3
    assert outcome.new_timestamp == "300"


def test_hitting_the_article_ceiling_still_advances_the_mark():
    # The case the old newest-first implementation deadlocked on: a backlog
    # larger than the ceiling meant the mark could never move at all.
    api = FakeAPI([
        [article("a", 100), article("b", 200)],
        [article("c", 300), article("d", 400)],
    ])
    outcome = run_engine(api, max_articles=2, batch_size=2)

    assert outcome.processed == 2
    assert outcome.new_timestamp == "200"          # next run resumes here
    assert len(api.fetch_calls) == 1


def test_mark_stops_before_the_first_failure():
    # 'c' fails. The mark must stop at 'b' — advancing to 'd' would skip 'c'
    # permanently, since everything above the mark is never re-examined.
    rules = [
        {"pattern": "good.com", "match_type": "domain", "tags": ["Good"]},
        {"pattern": "bad.com", "match_type": "domain", "tags": ["Bad"]},
    ]
    api = FakeAPI(
        [[
            article("a", 100, url="https://good.com/1"),
            article("b", 200, url="https://good.com/2"),
            article("c", 300, url="https://bad.com/3"),
            article("d", 400, url="https://good.com/4"),
        ]],
        tag_failures={"Bad"},
    )
    outcome = TaggerEngine(api, RunParameters(rules=rules, max_articles=10, batch_size=10)).run()

    assert outcome.status == STATUS_PARTIAL
    assert outcome.errors == 1
    assert outcome.new_timestamp == "200"


def test_failure_on_the_oldest_article_leaves_the_mark_untouched():
    api = FakeAPI([[article("a", 100), article("b", 200)]], tag_failures={"Example"})
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_PARTIAL
    assert outcome.new_timestamp is None


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
    # Nothing failed, so this article may still carry the mark forward.
    assert outcome.new_timestamp == "100"


def test_articles_without_a_url_are_skipped_cleanly():
    api = FakeAPI([[{"id": "a", "timestampUsec": "100", "categories": []}]])
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.skipped == 1
    assert outcome.new_timestamp == "100"


def test_empty_stream_is_a_no_op():
    api = FakeAPI([])
    outcome = run_engine(api, max_articles=10, batch_size=10)

    assert outcome.status == STATUS_SUCCESS
    assert outcome.processed == 0
    assert outcome.new_timestamp is None


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


def test_one_api_call_per_distinct_tag_not_per_article():
    # Writes are the scarce API zone, so batching by tag is a cost property
    # worth pinning down.
    api = FakeAPI([[article(f"a{i}", 100 + i) for i in range(10)]])
    outcome = run_engine(api, max_articles=50, batch_size=50)

    assert outcome.tagged == 10
    assert len(api.tag_calls) == 1
    assert api.tag_calls[0][0] == "Example"
    assert len(api.tag_calls[0][1]) == 10
