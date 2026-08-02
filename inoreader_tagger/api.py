"""Inoreader API client.

Behaviour is carried over from the original single-user script, with three
changes needed by the service: the redirect URI is configurable (it must match
what the hosted app registered), an expired refresh token raises a distinct
exception the caller can surface as "reconnect needed", and nothing prints —
callers collect a log instead.
"""

import logging
import secrets
import urllib.parse
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class InoreaderError(Exception):
    """Any failure talking to Inoreader."""


class InoreaderAuthError(InoreaderError):
    """The refresh token is expired or rejected — the user must reconnect."""


def _build_session() -> requests.Session:
    session = requests.Session()
    # Retry idempotent calls on transient upstream trouble. POSTs are excluded
    # by default in urllib3's allowed_methods, which is what we want: retrying
    # an edit-tag call that actually succeeded would be harmless but pointless.
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


class InoreaderAPI:
    """Wrapper for Inoreader API operations."""

    BASE_URL = "https://www.inoreader.com/reader/api/0"
    AUTH_URL = "https://www.inoreader.com/oauth2/token"
    AUTHORIZE_URL = "https://www.inoreader.com/oauth2/auth"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        refresh_token: Optional[str] = None,
        redirect_uri: str = "http://localhost",
        timeout: int = 30,
    ):
        self.app_id = app_id
        self.app_key = app_key
        self.access_token: Optional[str] = None
        self.refresh_token = refresh_token
        self.redirect_uri = redirect_uri
        self.timeout = timeout
        self._session = _build_session()

    # -- OAuth ---------------------------------------------------------------

    def get_authorization_url(self, state: Optional[str] = None) -> Tuple[str, str]:
        """Return (url, state). The caller must persist `state` and check it later."""
        state = state or secrets.token_urlsafe(32)
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "read write",
            "state": state,
        }
        return f"{self.AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state

    def exchange_code_for_token(self, auth_code: str) -> Dict:
        """Exchange an authorization code for access and refresh tokens.

        CSRF state validation happens in the web layer, which is what owns the
        session the state was stored in.
        """
        data = {
            "code": auth_code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.app_id,
            "client_secret": self.app_key,
            "grant_type": "authorization_code",
        }

        response = self._session.post(self.AUTH_URL, data=data, timeout=self.timeout)
        if response.status_code != 200:
            raise InoreaderError(f"Token exchange failed: {_describe(response)}")

        token_data = response.json()
        self.access_token = token_data["access_token"]
        self.refresh_token = token_data["refresh_token"]
        return token_data

    def refresh_access_token(self) -> Dict:
        """Mint a new access token. Raises InoreaderAuthError if the grant is dead."""
        if not self.refresh_token:
            raise InoreaderAuthError("No refresh token stored — reconnect required")

        data = {
            "refresh_token": self.refresh_token,
            "client_id": self.app_id,
            "client_secret": self.app_key,
            "grant_type": "refresh_token",
        }

        response = self._session.post(self.AUTH_URL, data=data, timeout=self.timeout)

        if response.status_code != 200:
            # 400/401 mean the grant itself is gone. Anything else is more
            # likely an outage, and treating that as "user must reconnect"
            # would send people to re-authorize for no reason.
            if response.status_code in (400, 401):
                raise InoreaderAuthError(
                    f"Refresh token rejected: {_describe(response)}"
                )
            raise InoreaderError(f"Token refresh failed: {_describe(response)}")

        token_data = response.json()
        self.access_token = token_data["access_token"]
        # Inoreader may hand back a rotated refresh token; keep it if so.
        if token_data.get("refresh_token"):
            self.refresh_token = token_data["refresh_token"]
        return token_data

    def _headers(self) -> Dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    # Retained under its original name because migrate_tags.py calls it.
    _get_headers = _headers

    # -- Reads ---------------------------------------------------------------

    def get_stream_contents(
        self,
        stream_id: str = "user/-/state/com.google/reading-list",
        count: int = 100,
        continuation: Optional[str] = None,
    ) -> Dict:
        """Raw paged access to any stream. Used by the tag migration tool."""
        params = {"n": count, "output": "json"}
        if continuation:
            params["c"] = continuation

        response = self._session.get(
            f"{self.BASE_URL}/stream/contents/{stream_id}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise InoreaderAuthError("Access token rejected fetching stream contents")
        if response.status_code != 200:
            raise InoreaderError(f"Could not fetch stream: {_describe(response)}")
        return response.json()

    def get_user_info(self) -> Dict:
        """Identity of the authenticated user — this is how accounts are keyed."""
        response = self._session.get(
            f"{self.BASE_URL}/user-info",
            headers=self._headers(),
            params={"output": "json"},
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise InoreaderAuthError("Access token rejected fetching user info")
        if response.status_code != 200:
            raise InoreaderError(f"Could not fetch user info: {_describe(response)}")
        return response.json()

    def get_unread_articles(
        self,
        count: int = 100,
        folder_name: Optional[str] = None,
        since_timestamp: Optional[str] = None,
        continuation: Optional[str] = None,
        oldest_first: bool = True,
    ) -> Tuple[List[Dict], Optional[str]]:
        """Fetch a page of unread articles.

        Returns (articles, continuation). A continuation of None means the end
        of the stream was reached — that is the only reliable "we have seen
        everything" signal, and it is what lets the caller safely advance the
        high-water mark.

        `oldest_first` asks the server for ascending order (`r=o`). That
        matters: processing oldest-first makes the high-water mark a resumable
        cursor, because everything below it is genuinely done. Newest-first
        leaves a hole between the mark and the oldest article of the page, so
        the mark can never move while a backlog exists.
        """
        if folder_name:
            encoded_folder = urllib.parse.quote(folder_name, safe="")
            stream_id = f"user/-/label/{encoded_folder}"
        else:
            stream_id = "user/-/state/com.google/reading-list"

        params = {
            "n": count,
            "output": "json",
            # Server-side exclusion of read items, so read articles are never
            # touched regardless of what the rules match.
            "xt": "user/-/state/com.google/read",
        }

        if oldest_first:
            params["r"] = "o"

        if continuation:
            params["c"] = continuation

        client_filter_timestamp = None
        if since_timestamp:
            params["ot"] = str(int(since_timestamp))
            client_filter_timestamp = int(since_timestamp)

        response = self._session.get(
            f"{self.BASE_URL}/stream/contents/{stream_id}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise InoreaderAuthError("Access token rejected fetching articles")
        if response.status_code != 200:
            raise InoreaderError(f"Could not fetch articles: {_describe(response)}")

        payload = response.json()
        articles = payload.get("items", [])

        # `ot` is honoured server-side but is documented as approximate; filter
        # again locally so a run can never reprocess what it already handled.
        if client_filter_timestamp is not None:
            articles = [
                article
                for article in articles
                if _safe_int(article.get("timestampUsec")) > client_filter_timestamp
            ]

        return articles, payload.get("continuation")

    def get_tags(self) -> List[Dict]:
        response = self._session.get(
            f"{self.BASE_URL}/tag/list",
            headers=self._headers(),
            params={"output": "json"},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise InoreaderError(f"Could not fetch tags: {_describe(response)}")
        return [tag for tag in response.json().get("tags", []) if "/label/" in tag.get("id", "")]

    # -- Writes --------------------------------------------------------------

    def add_tag_to_articles_batch(self, article_ids: List[str], tag_name: str) -> Tuple[bool, str]:
        """Apply one tag to many articles in a single call."""
        if not article_ids:
            return True, ""

        if not tag_name.startswith("user/-/label/"):
            tag_name = f"user/-/label/{tag_name}"

        data = [("a", tag_name), ("ac", "edit-tags")]
        data.extend(("i", article_id) for article_id in article_ids)

        response = self._session.post(
            f"{self.BASE_URL}/edit-tag",
            headers=self._headers(),
            data=data,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise InoreaderAuthError("Access token rejected applying tags")
        if response.status_code == 200:
            return True, ""
        return False, _describe(response)

    def remove_tag_from_articles_batch(self, article_ids: List[str], tag_name: str) -> Tuple[bool, str]:
        if not article_ids:
            return True, ""

        if not tag_name.startswith("user/-/label/"):
            tag_name = f"user/-/label/{tag_name}"

        data = [("r", tag_name), ("ac", "edit-tags")]
        data.extend(("i", article_id) for article_id in article_ids)

        response = self._session.post(
            f"{self.BASE_URL}/edit-tag",
            headers=self._headers(),
            data=data,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise InoreaderAuthError("Access token rejected removing tags")
        if response.status_code == 200:
            return True, ""
        return False, _describe(response)


def _describe(response: requests.Response) -> str:
    detail = f"HTTP {response.status_code}"
    try:
        body = response.text.strip()
    except Exception:  # pragma: no cover - body already consumed/undecodable
        return detail
    if body:
        detail += f": {body[:300]}"
    return detail


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default
