"""End-to-end tests over the HTTP surface, including the run/DB round trip."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inoreader_tagger.api import InoreaderAuthError
from inoreader_tagger.db import STATUS_AUTH_REQUIRED, STATUS_SUCCESS, Run, User
from inoreader_tagger.defaults import DEFAULT_RULES


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "DELETE")
    monkeypatch.setenv("INOREADER_APP_ID", "test-app-id")
    monkeypatch.setenv("INOREADER_APP_KEY", "test-app-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://tagger.test")

    from inoreader_tagger.web import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client, app


def make_user(app, **overrides) -> int:
    database = app.state.database
    cipher = app.state.cipher
    session = database.session()
    try:
        user = User(
            inoreader_user_id=overrides.pop("inoreader_user_id", "1000123"),
            email=overrides.pop("email", "reader@example.com"),
            refresh_token_encrypted=cipher.encrypt("stored-refresh-token"),
            interval_minutes=30,
            max_articles=200,
            batch_size=100,
        )
        user.rules = DEFAULT_RULES
        for key, value in overrides.items():
            setattr(user, key, value)
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


def test_probes_report_healthy(app_env):
    client, _ = app_env
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ok"}


def test_empty_status_page_invites_a_connection(app_env):
    client, _ = app_env
    response = client.get("/")
    assert response.status_code == 200
    assert "Nothing connected yet" in response.text


def test_status_page_lists_a_connected_account(app_env):
    client, app = app_env
    make_user(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "reader@example.com" in response.text
    assert "Connected" in response.text


def test_reauth_needed_is_surfaced_prominently(app_env):
    client, app = app_env
    make_user(app, needs_reauth=True)

    body = client.get("/").text
    assert "Attention needed" in body
    assert "Re-login needed" in body


def test_api_status_reports_reauth_for_scripting(app_env):
    client, app = app_env
    make_user(app, needs_reauth=True)

    payload = client.get("/api/status").json()
    assert payload["healthy"] is False
    assert payload["needs_reauth"] == ["reader@example.com"]


def test_rules_must_be_valid_json_to_save(app_env):
    client, app = app_env
    user_id = make_user(app)

    response = client.post(f"/users/{user_id}/rules", data={"rules": "{not json"}, follow_redirects=True)
    assert "not valid JSON" in response.text


def test_rules_are_validated_before_saving(app_env):
    client, app = app_env
    user_id = make_user(app)

    bad = '[{"pattern": "x", "match_type": "banana", "tags": ["A"]}]'
    response = client.post(f"/users/{user_id}/rules", data={"rules": bad}, follow_redirects=True)
    assert "match_type" in response.text

    session = app.state.database.session()
    try:
        user = session.get(User, user_id)
        assert len(user.rules) == len(DEFAULT_RULES)  # unchanged
    finally:
        session.close()


def test_valid_rules_are_persisted(app_env):
    client, app = app_env
    user_id = make_user(app)

    good = '[{"pattern": "lwn.net", "match_type": "domain", "tags": ["LWN"]}]'
    client.post(f"/users/{user_id}/rules", data={"rules": good}, follow_redirects=True)

    session = app.state.database.session()
    try:
        assert session.get(User, user_id).rules == [
            {"pattern": "lwn.net", "match_type": "domain", "tags": ["LWN"]}
        ]
    finally:
        session.close()


def test_settings_reject_a_batch_larger_than_the_ceiling(app_env):
    client, app = app_env
    user_id = make_user(app)

    response = client.post(
        f"/users/{user_id}/settings",
        data={
            "interval_minutes": "15",
            "max_articles": "50",
            "batch_size": "500",
            "folder_filter": "",
            "enabled": "on",
        },
        follow_redirects=True,
    )
    assert "Batch size cannot exceed max articles" in response.text


def test_settings_save_and_unchecked_boxes_mean_off(app_env):
    client, app = app_env
    user_id = make_user(app)

    client.post(
        f"/users/{user_id}/settings",
        data={
            "interval_minutes": "15",
            "max_articles": "50",
            "batch_size": "25",
            "folder_filter": "Tech News",
            # 'enabled' and 'dry_run' omitted, as a browser omits unchecked boxes
        },
        follow_redirects=True,
    )

    session = app.state.database.session()
    try:
        user = session.get(User, user_id)
        assert user.interval_minutes == 15
        assert user.folder_filter == "Tech News"
        assert user.enabled is False
        assert user.dry_run is False
    finally:
        session.close()


def test_delete_requires_matching_confirmation(app_env):
    client, app = app_env
    user_id = make_user(app)

    client.post(f"/users/{user_id}/delete", data={"confirm": "wrong"}, follow_redirects=True)
    session = app.state.database.session()
    try:
        assert session.get(User, user_id) is not None
    finally:
        session.close()

    client.post(
        f"/users/{user_id}/delete", data={"confirm": "reader@example.com"}, follow_redirects=True
    )
    session = app.state.database.session()
    try:
        assert session.get(User, user_id) is None
    finally:
        session.close()


def test_run_records_success_and_advances_the_mark(app_env, monkeypatch):
    client, app = app_env
    user_id = make_user(app)

    class FakeAPI:
        def __init__(self, *_, **kwargs):
            self.refresh_token = kwargs.get("refresh_token")

        def refresh_access_token(self):
            return {}

        def get_unread_articles(self, count, folder_name=None, since_timestamp=None,
                                continuation=None, oldest_first=True):
            if continuation is not None:
                return [], None
            return [
                {
                    "id": "art-1",
                    "timestampUsec": "1700000000000000",
                    "canonical": [{"href": "https://github.com/foo/bar"}],
                    "categories": [],
                }
            ], None

        def add_tag_to_articles_batch(self, ids, tag):
            return True, ""

    monkeypatch.setattr("inoreader_tagger.runner.InoreaderAPI", FakeAPI)

    run_id = app.state.run_service.execute(user_id, manual=True)

    session = app.state.database.session()
    try:
        run = session.get(Run, run_id)
        assert run.status == STATUS_SUCCESS
        assert run.tagged == 1
        assert run.triggered_manually is True
        assert session.get(User, user_id).last_processed_timestamp == "1700000000000000"
    finally:
        session.close()


def test_run_with_dead_token_flags_the_user_for_reauth(app_env, monkeypatch):
    client, app = app_env
    user_id = make_user(app)

    class DeadAPI:
        def __init__(self, *_, **kwargs):
            self.refresh_token = kwargs.get("refresh_token")

        def refresh_access_token(self):
            raise InoreaderAuthError("refresh token revoked")

    monkeypatch.setattr("inoreader_tagger.runner.InoreaderAPI", DeadAPI)

    run_id = app.state.run_service.execute(user_id)

    session = app.state.database.session()
    try:
        assert session.get(Run, run_id).status == STATUS_AUTH_REQUIRED
        assert session.get(User, user_id).needs_reauth is True
    finally:
        session.close()

    # And the operator can see it without digging.
    assert client.get("/api/status").json()["healthy"] is False


def test_run_history_is_pruned_to_the_configured_limit(app_env, monkeypatch):
    client, app = app_env
    user_id = make_user(app)

    # Settings is a frozen dataclass, so swap the whole object on the service.
    from dataclasses import replace

    app.state.run_service.settings = replace(app.state.settings, run_history_limit=3)

    class NoopAPI:
        def __init__(self, *_, **kwargs):
            self.refresh_token = kwargs.get("refresh_token")

        def refresh_access_token(self):
            return {}

        def get_unread_articles(self, *a, **k):
            return [], None

    monkeypatch.setattr("inoreader_tagger.runner.InoreaderAPI", NoopAPI)

    for _ in range(6):
        app.state.run_service.execute(user_id)

    session = app.state.database.session()
    try:
        runs = session.scalars(select(Run).where(Run.user_id == user_id)).all()
        assert len(runs) == 3
    finally:
        session.close()


def test_oauth_callback_rejects_a_mismatched_state(app_env):
    client, _ = app_env
    response = client.get(
        "/auth/callback", params={"code": "abc", "state": "forged"}, follow_redirects=True
    )
    assert "state mismatch" in response.text
