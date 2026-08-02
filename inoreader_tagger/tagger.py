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
    # Obsolete. It used to override the article-ceiling rule that could stop
    # the high-water mark advancing. Processing oldest-first removed the need
    # for both the rule and this escape hatch. Kept so existing callers do not
    # break; it has no effect.
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
        total_fetched = 0
        continuation: Optional[str] = None
        reached_end = False

        # The mark may only advance across an unbroken run of successes,
        # starting from the oldest article. Once anything fails it freezes:
        # every later article is newer, so moving past the failure would skip
        # it permanently.
        safe_mark: Optional[str] = None
        mark_frozen = False

        while total_fetched < params.max_articles:
            fetch_count = min(params.batch_size, params.max_articles - total_fetched)

            articles, continuation = self.api.get_unread_articles(
                count=fetch_count,
                folder_name=params.folder_filter,
                since_timestamp=since,
                continuation=continuation,
            )

            if not articles:
                reached_end = True
                if total_fetched == 0:
                    self._say("No new articles to process.")
                break

            self._say(f"Processing batch of {len(articles)} articles.")
            batch = self._process_batch(articles)

            processed += batch.processed
            tagged += batch.tagged
            skipped += batch.skipped
            errors += batch.errors
            total_fetched += len(articles)

            # Articles arrive oldest-first, so walking them in order and
            # stopping at the first failure gives the newest point everything
            # below which is genuinely done.
            for article, clean in zip(articles, batch.clean_flags):
                if not clean:
                    mark_frozen = True
                    break
                stamp = _safe_int(article.get("timestampUsec"))
                if stamp:
                    safe_mark = str(stamp)
            if mark_frozen:
                break

            # An absent continuation is the only trustworthy end-of-stream
            # signal. A short page does not mean the stream is exhausted.
            if not continuation:
                reached_end = True
                self._say(f"Reached the end of the stream ({total_fetched} article(s) seen).")
                break

            time.sleep(0.5)

        if not reached_end and not mark_frozen and total_fetched >= params.max_articles:
            self._say(
                f"Stopped at the {params.max_articles}-article ceiling with more still "
                "available. The next run resumes from the new high-water mark."
            )

        outcome = RunOutcome(
            status=STATUS_SUCCESS,
            processed=processed,
            tagged=tagged,
            skipped=skipped,
            errors=errors,
            log_lines=self._log,
        )

        outcome.new_timestamp = self._decide_new_timestamp(
            safe_mark=safe_mark,
            since=since,
            mark_frozen=mark_frozen,
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
        safe_mark: Optional[str],
        since: Optional[str],
        mark_frozen: bool,
    ) -> Optional[str]:
        """Return the new high-water mark, or None to leave it untouched.

        There is deliberately no article-ceiling rule any more. Because
        articles are processed oldest-first, stopping early leaves a contiguous
        processed prefix, and advancing to the end of that prefix is always
        safe — the next run simply resumes from there. Under the old
        newest-first fetch the same situation left an unprocessed hole *below*
        the newest article, which is why advancing had to be refused, and why a
        backlog larger than the ceiling could deadlock the mark forever.
        """
        if self.params.dry_run:
            self._say("Dry run — high-water mark left unchanged.")
            return None

        if safe_mark is None:
            if mark_frozen:
                self._say(
                    "High-water mark unchanged: the oldest article this run could not "
                    "be tagged. It will be retried next run."
                )
            return None

        # Never move backwards.
        if since and int(safe_mark) <= int(since):
            self._say("High-water mark unchanged: nothing newer was processed cleanly.")
            return None

        if mark_frozen:
            self._say(
                f"Advancing high-water mark to {safe_mark} — stopping short of the first "
                "article that errored, so it is retried rather than skipped."
            )
        else:
            self._say(f"Advancing high-water mark to {safe_mark}.")
        return safe_mark

    def _process_batch(self, articles: List[Dict]) -> "_BatchResult":
        """Match a batch and apply its tags, one API call per distinct tag."""
        # tag -> list of article indices that need it
        tag_to_indices: Dict[str, List[int]] = {}
        # index -> article, for those that need at least one tag
        needs_tagging: Dict[int, Dict] = {}
        # One flag per input article, in the same order. "Clean" means nothing
        # failed for it — including articles that simply had no matching rule.
        # The caller walks these in order to find how far the mark may advance.
        clean_flags: List[bool] = [True] * len(articles)
        skipped = 0

        for index, article in enumerate(articles):
            url = _canonical_url(article)

            if not url:
                skipped += 1
                # No URL means nothing to match, but nothing failed either, so
                # it must not hold the high-water mark back.
                continue

            wanted = self.matcher.match_url(url)
            if not wanted:
                skipped += 1
                continue

            existing = {
                category.split("/label/")[-1]
                for category in article.get("categories", [])
                if "/label/" in category
            }
            to_add = [tag for tag in wanted if tag not in existing]

            if not to_add:
                skipped += 1
                continue

            needs_tagging[index] = article
            for tag in to_add:
                tag_to_indices.setdefault(tag, []).append(index)

        if not tag_to_indices:
            self._say(f"  Nothing to tag in this batch ({skipped} skipped).")
            return _BatchResult(
                processed=len(articles), tagged=0, skipped=skipped, errors=0,
                clean_flags=clean_flags,
            )

        for tag, indices in sorted(tag_to_indices.items()):
            self._say(f"  Tag {tag!r}: {len(indices)} article(s)")

        if self.params.dry_run:
            self._say(f"  [DRY RUN] would make {len(tag_to_indices)} tag call(s).")
            return _BatchResult(
                processed=len(articles), tagged=len(needs_tagging), skipped=skipped, errors=0,
                clean_flags=clean_flags,
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

        for index in failed_indices:
            clean_flags[index] = False

        tagged = len(needs_tagging) - len(failed_indices)
        return _BatchResult(
            processed=len(articles),
            tagged=tagged,
            skipped=skipped,
            errors=errors,
            clean_flags=clean_flags,
        )


@dataclass
class _BatchResult:
    processed: int
    tagged: int
    skipped: int
    errors: int
    clean_flags: List[bool]


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
