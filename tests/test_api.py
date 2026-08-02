"""Tests for the HTTP client, covering the request it builds and the filtering
it applies to the response.

These are the details that decide how much of a small API quota a run costs,
and whether an article can be silently dropped.
"""

import pytest

from inoreader_tagger.api import InoreaderAPI, InoreaderAuthError, InoreaderError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    """Records the requests made and replays canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self._responses.pop(0)

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"url": url, "data": data})
        return self._responses.pop(0)

    def mount(self, *args, **kwargs):  # pragma: no cover - unused
        pass


def make_api(responses):
    api = InoreaderAPI("app-id", "app-key", refresh_token="tok")
    api._session = FakeSession(responses)
    api.access_token = "access"
    return api


def item(article_id, timestamp):
    return {"id": article_id, "timestampUsec": str(timestamp), "categories": []}


def test_requests_oldest_first_and_excludes_read_items():
    api = make_api([FakeResponse({"items": [], "continuation": None})])
    api.get_unread_articles(count=50)

    params = api._session.calls[0]["params"]
    # Ascending order is what makes the high-water mark a resumable cursor.
    assert params["r"] == "o"
    # Read articles must never be touched, and this is enforced server-side.
    assert params["xt"] == "user/-/state/com.google/read"
    assert params["n"] == 50
    assert "c" not in params


def test_continuation_is_sent_and_returned():
    api = make_api([FakeResponse({"items": [item("a", 100)], "continuation": "next-page"})])
    articles, continuation = api.get_unread_articles(count=10, continuation="prev-page")

    assert api._session.calls[0]["params"]["c"] == "prev-page"
    assert continuation == "next-page"
    assert [a["id"] for a in articles] == ["a"]


def test_absent_continuation_means_end_of_stream():
    api = make_api([FakeResponse({"items": [item("a", 100)]})])
    _, continuation = api.get_unread_articles(count=10)

    assert continuation is None


def test_articles_older_than_the_mark_are_filtered_out():
    api = make_api([FakeResponse({"items": [item("old", 50), item("new", 300)]})])
    articles, _ = api.get_unread_articles(count=10, since_timestamp="100")

    assert [a["id"] for a in articles] == ["new"]


def test_an_article_sharing_the_marks_timestamp_is_kept():
    """Inoreader can give several articles the same timestampUsec.

    The mark is a timestamp, not an article id. Filtering it out exclusively
    would permanently drop any sibling ingested in the same crawl, so the
    boundary is inclusive: the already-processed article comes back and is
    skipped as already tagged, which costs nothing.
    """
    api = make_api([FakeResponse({"items": [item("already-done", 100), item("sibling", 100)]})])
    articles, _ = api.get_unread_articles(count=10, since_timestamp="100")

    assert [a["id"] for a in articles] == ["already-done", "sibling"]


def test_folder_filter_scopes_the_stream():
    api = make_api([FakeResponse({"items": []})])
    api.get_unread_articles(count=10, folder_name="Discussion sites")

    assert "user/-/label/Discussion%20sites" in api._session.calls[0]["url"]


def test_a_401_while_fetching_is_an_auth_error_not_a_generic_one():
    api = make_api([FakeResponse({}, status_code=401)])
    with pytest.raises(InoreaderAuthError):
        api.get_unread_articles(count=10)


def test_a_500_while_fetching_is_not_an_auth_error():
    api = make_api([FakeResponse({}, status_code=500)])
    with pytest.raises(InoreaderError) as excinfo:
        api.get_unread_articles(count=10)
    assert not isinstance(excinfo.value, InoreaderAuthError)


def test_rejected_refresh_token_is_distinguished_from_an_outage():
    # 400/401 means the grant is gone and the user must reconnect.
    api = make_api([FakeResponse({"error": "invalid_grant"}, status_code=400)])
    with pytest.raises(InoreaderAuthError):
        api.refresh_access_token()

    # 503 is an outage; telling someone to re-authorize would be wrong.
    api = make_api([FakeResponse({}, status_code=503)])
    with pytest.raises(InoreaderError) as excinfo:
        api.refresh_access_token()
    assert not isinstance(excinfo.value, InoreaderAuthError)


def test_tagging_sends_one_call_for_many_articles():
    api = make_api([FakeResponse({}, status_code=200)])
    ok, detail = api.add_tag_to_articles_batch(["a", "b", "c"], "Example")

    assert ok and detail == ""
    data = api._session.calls[0]["data"]
    assert ("a", "user/-/label/Example") == ("a", dict(data)["a"])
    assert [v for k, v in data if k == "i"] == ["a", "b", "c"]
