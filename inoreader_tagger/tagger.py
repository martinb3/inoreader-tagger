"""The tagging engine: fetch unread articles, match rules, apply tags.

This is the original script's logic, restructured so a run returns a result
object instead of printing and writing files. The caller owns persistence,
which is what lets one process run this for many users.

Timestamp handling is the delicate part. `last_processed_timestamp` is the
high-water mark passed to Inoreader as `ot` on the next run. Advancing it past
an article that failed to tag would silently skip that article forever, so the
mark only moves to the newest article that was fully processed without error.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .api import InoreaderAPI, InoreaderAuthError, InoreaderError
from .db import STATUS_AUTH_REQUIRED, STATUS_FAILED, STATUS_PARTIAL, STATUS_SUCCESS
from .matcher import URLPatternMatcher

logger = logging.getLogger(__name__)

MAX_LOG_CHARS = 20000


@dataclass
class RunOutcome:
    """Everything the caller needs to record about one run."""

    status: str
    processed: int = 0
    tagged: int = 0
    skipped: int = 0
    errors: int = 0
    new_timestamp: Optional[str] = None
    error_message: Optional[str] = None
    log_lines: List[str] = field(default_factory=list)

    @property
    def log(self) -> str:
        text = "\n".join(self.log_lines)
        if len(text) > MAX_LOG_CHARS:
            # Keep the tail: the end of a run explains why it ended.
            text = "... (log truncated) ...\n" + text[-MAX_LOG_CHARS:]
        return text


@dataclass
class RunParameters:
    """The per-user knobs a run needs, decoupled from the ORM model."""

    rules: List[dict]
    max_articles: int = 200
    batch_size: int = 100
    folder_filter: Optional[str] = None
    dry_run: bool = False
    since_timestamp: Optional[str] = None
    force_timestamp_update: bool = False


class TaggerEngine:
    """Runs one tagging pass for one authenticated account."""

    def __init__(self, api: InoreaderAPI, params: RunParameters):
        self.api = api
        self.params = params
        self.matcher = URLPatternMatcher(params.rules)
        self._log: List[str] = []

    def _say(self, message: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
        self._log.append(f"[{stamp}] {message}")

    def run(self) -> RunOutcome:
        params = self.params

        if not params.rules:
            self._say("No tagging rules configured — nothing to do.")
            return RunOutcome(status=STATUS_SUCCESS, log_lines=self._log)

        try:
            self.api.refresh_access_token()
            self._say("Access token refreshed.")
        except InoreaderAuthError as exc:
            self._say(f"AUTH FAILED: {exc}")
            return RunOutcome(
                status=STATUS_AUTH_REQUIRED,
                error_message=str(exc),
                log_lines=self._log,
            )
        except InoreaderError as exc:
            self._say(f"Could not refresh token: {exc}")
            return RunOutcome(status=STATUS_FAILED, error_message=str(exc), log_lines=self._log)

        try:
            return self._process()
        except InoreaderAuthError as exc:
            self._say(f"AUTH FAILED mid-run: {exc}")
            return RunOutcome(
                status=STATUS_AUTH_REQUIRED,
                error_message=str(exc),
                log_lines=self._log,
            )
        except Exception as exc:  # noqa: BLE001 - a run must never kill the scheduler
            logger.exception("Unhandled error during tagging run")
            self._say(f"UNEXPECTED ERROR: {exc}")
            return RunOutcome(status=STATUS_FAILED, error_message=str(exc), log_lines=self._log)

    def _process(self) -> RunOutcome:
        params = self.params
        since = params.since_timestamp

        if since:
            self._say(f"Fetching unread articles newer than timestamp {since}.")
        else:
            self._say("No previous high-water mark — fetching recent unread articles.")

        if params.folder_filter:
            self._say(f"Restricted to folder {params.folder_filter!r}.")

        processed = tagged = skipped = errors = 0
        newest_clean_timestamp: Optional[str] = None
        total_fetched = 0

        while total_fetched < params.max_articles:
            remaining = params.max_articles - total_fetched
            fetch_count = min(params.batch_size, remaining)

            articles = self.api.get_unread_articles(
                count=fetch_count,
                folder_name=params.folder_filter,
                since_timestamp=since,
            )

            if not articles:
                if total_fetched == 0:
                    self._say("No new articles to process.")
                break

            self._say(f"Processing batch of {len(articles)} articles.")
            batch = self._process_batch(articles)

            processed += batch.processed
            tagged += batch.tagged
            skipped += batch.skipped
            errors += batch.errors

            # Only articles that came through with no error can move the mark.
            for article in batch.clean_articles:
                stamp = _safe_int(article.get("timestampUsec"))
                if stamp and (newest_clean_timestamp is None or stamp > int(newest_clean_timestamp)):
                    newest_clean_timestamp = str(stamp)

            total_fetched += len(articles)

            if len(articles) < fetch_count:
                self._say(f"Reached the end of unread articles ({total_fetched} seen).")
                break

            if total_fetched < params.max_articles:
                time.sleep(0.5)

        outcome = RunOutcome(
            status=STATUS_SUCCESS,
            processed=processed,
            tagged=tagged,
            skipped=skipped,
            errors=errors,
            log_lines=self._log,
        )

        outcome.new_timestamp = self._decide_new_timestamp(
            newest_clean_timestamp=newest_clean_timestamp,
            since=since,
            total_fetched=total_fetched,
            errors=errors,
        )

        if errors:
            outcome.status = STATUS_PARTIAL
            outcome.error_message = f"{errors} tag operation(s) failed"

        self._say(
            f"Done. processed={processed} tagged={tagged} skipped={skipped} errors={errors}"
        )
        return outcome

    def _decide_new_timestamp(
        self,
        newest_clean_timestamp: Optional[str],
        since: Optional[str],
        total_fetched: int,
        errors: int,
    ) -> Optional[str]:
        """Return the new high-water mark, or None to leave it untouched."""
        params = self.params

        if params.dry_run:
            self._say("Dry run — high-water mark left unchanged.")
            return None

        if not newest_clean_timestamp:
            if errors:
                self._say(
                    "High-water mark unchanged: every article this run hit an error. "
                    "They will be retried next run."
                )
            return None

        # Never move backwards.
        if since and int(newest_clean_timestamp) <= int(since):
            self._say("High-water mark unchanged: no newer articles were processed.")
            return None

        # Hitting the ceiling means there may be unfetched articles older than
        # the newest one we just handled. Advancing would skip them.
        if total_fetched >= params.max_articles and not params.force_timestamp_update:
            self._say(
                f"High-water mark unchanged: hit the {params.max_articles}-article ceiling, "
                "so older unread articles may remain. Next run will pick them up."
            )
            return None

        if total_fetched >= params.max_articles and params.force_timestamp_update:
            self._say(
                "WARNING: advancing the high-water mark despite hitting the article "
                "ceiling — some articles may be skipped."
            )

        self._say(f"Advancing high-water mark to {newest_clean_timestamp}.")
        return newest_clean_timestamp

    def _process_batch(self, articles: List[Dict]) -> "_BatchResult":
        """Match a batch and apply its tags, one API call per distinct tag."""
        # tag -> list of article indices that need it
        tag_to_indices: Dict[str, List[int]] = {}
        # index -> article, for those that need at least one tag
        needs_tagging: Dict[int, Dict] = {}
        clean_articles: List[Dict] = []
        skipped = 0

        for index, article in enumerate(articles):
            url = _canonical_url(article)

            if not url:
                skipped += 1
                # No URL means nothing to match, but nothing failed either, so
                # it must not hold the high-water mark back.
                clean_articles.append(article)
                continue

            wanted = self.matcher.match_url(url)
            if not wanted:
                skipped += 1
                clean_articles.append(article)
                continue

            existing = {
                category.split("/label/")[-1]
                for category in article.get("categories", [])
                if "/label/" in category
            }
            to_add = [tag for tag in wanted if tag not in existing]

            if not to_add:
                skipped += 1
                clean_articles.append(article)
                continue

            needs_tagging[index] = article
            for tag in to_add:
                tag_to_indices.setdefault(tag, []).append(index)

        if not tag_to_indices:
            self._say(f"  Nothing to tag in this batch ({skipped} skipped).")
            return _BatchResult(
                processed=len(articles), tagged=0, skipped=skipped, errors=0,
                clean_articles=clean_articles,
            )

        for tag, indices in sorted(tag_to_indices.items()):
            self._say(f"  Tag {tag!r}: {len(indices)} article(s)")

        if self.params.dry_run:
            self._say(f"  [DRY RUN] would make {len(tag_to_indices)} tag call(s).")
            clean_articles.extend(needs_tagging.values())
            return _BatchResult(
                processed=len(articles), tagged=len(needs_tagging), skipped=skipped, errors=0,
                clean_articles=clean_articles,
            )

        # Track failures per article so a single bad tag doesn't discard the
        # whole batch's progress, and so partial success is reported honestly.
        failed_indices = set()
        errors = 0

        for tag, indices in sorted(tag_to_indices.items()):
            article_ids = [articles[i].get("id", "") for i in indices]
            try:
                ok, detail = self.api.add_tag_to_articles_batch(article_ids, tag)
            except InoreaderAuthError:
                raise
            except InoreaderError as exc:
                ok, detail = False, str(exc)

            if ok:
                self._say(f"  Applied {tag!r} to {len(article_ids)} article(s).")
            else:
                errors += 1
                failed_indices.update(indices)
                self._say(f"  FAILED to apply {tag!r}: {detail}")

        for index, article in needs_tagging.items():
            if index not in failed_indices:
                clean_articles.append(article)

        tagged = len(needs_tagging) - len(failed_indices)
        return _BatchResult(
            processed=len(articles),
            tagged=tagged,
            skipped=skipped,
            errors=errors,
            clean_articles=clean_articles,
        )


@dataclass
class _BatchResult:
    processed: int
    tagged: int
    skipped: int
    errors: int
    clean_articles: List[Dict]


def _canonical_url(article: Dict) -> str:
    canonical = article.get("canonical") or []
    if canonical:
        return canonical[0].get("href", "")
    alternate = article.get("alternate") or []
    if alternate:
        return alternate[0].get("href", "")
    return ""


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default
