# Agent Rules for the Inoreader Tagger Project

Context for AI assistants working on this repository.

## What this is

A Python service that applies tags to Inoreader articles based on URL patterns.
It runs either as a scheduled multi-account web service with a status page, or
as a single-account CLI reading `config.json`.

**This repository is public.** No credentials, no real account identifiers, no
internal hostnames from any private deployment belong in it.

## Layout

| Path | Responsibility |
|---|---|
| `inoreader_tagger/api.py` | Inoreader HTTP client and OAuth |
| `inoreader_tagger/matcher.py` | URL → tags; `validate_rules()` for UI input |
| `inoreader_tagger/tagger.py` | Run engine; owns high-water mark logic |
| `inoreader_tagger/runner.py` | Executes a run and records it to the database |
| `inoreader_tagger/scheduler.py` | Per-account APScheduler jobs |
| `inoreader_tagger/web.py` | FastAPI app: status page, OAuth, settings |
| `inoreader_tagger/db.py` | SQLAlchemy models and engine setup |
| `inoreader_tagger/crypto.py` | Fernet encryption for refresh tokens |
| `inoreader_tagger/config.py` | Environment-variable settings |
| `inoreader_tagger/cli.py` | Single-account command line |
| `migrate_tags.py` | Standalone one-off tag reshaping tool |
| `tests/` | pytest suite |

## The things that are easy to break

### The high-water mark

`User.last_processed_timestamp` is passed to Inoreader as `ot` on the next run,
so **anything older is never examined again**. Advancing it past an article
that was not successfully tagged loses that article silently — no error, no
retry, nothing in the logs.

`TaggerEngine._decide_new_timestamp()` is the only place allowed to move it.
The rules it enforces:

- advance only to the newest article processed with **zero** errors
- never advance on a dry run
- never advance backwards
- never advance when the run hit its article ceiling (older unread articles may
  remain unfetched), unless `force_timestamp_update` is set

Every one of those has a test in `tests/test_tagger.py`. If you change this
logic, the tests should change deliberately, not incidentally.

### Auth failure is a distinct outcome

`STATUS_AUTH_REQUIRED` is not the same as `STATUS_FAILED`. It means a human
must go and re-authorize, and it is what drives the status page's re-login
badge — the entire point of the status page. Only a 400/401 from the token
endpoint should produce it; a 500 or a timeout is `STATUS_FAILED`, because
telling someone to re-authorize during an Inoreader outage is wrong.

### Read articles

Article fetching passes `xt=user/-/state/com.google/read` so read articles are
excluded server-side. Do not remove this. Tagging articles someone has already
read is the failure mode users notice most.

### Single instance

The scheduler and SQLite both assume one process. `workers=1` in
`__main__.py`, `replicas: 1` and `strategy: Recreate` in any deployment. Two
instances double-tag every article.

## Conventions

- Type hints on new functions; the codebase already uses them.
- Comments explain *why*, not *what*. Existing comments follow this — match it.
- No `print()` outside `cli.py`. The engine collects log lines into
  `RunOutcome.log_lines` so they can be stored and shown in the UI.
- Keep `--dry-run` working end to end.
- Rules are validated in `matcher.validate_rules()`, once, and used by both the
  web UI and anything else that accepts rule input.

## Security

- Refresh tokens are encrypted at rest. Never log them, never render them in a
  template, never add them to `/api/status`.
- `ENCRYPTION_KEY` losing its value orphans every stored token. Anything that
  touches key handling needs care.
- The service has **no authentication** by design. Do not add a feature that
  assumes it does — e.g. anything destructive that is only guarded by "the user
  clicked it".
- OAuth `state` is validated against the session cookie. Keep it.

## Testing

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Tests use a fake API client rather than hitting Inoreader. When adding
behaviour, add the test that would catch its absence — particularly for
anything touching the high-water mark or run status.

Manual checks worth doing before shipping a change to the run engine:

- a dry run leaves the mark untouched
- an account with a revoked token shows the re-login badge
- a rule with a bad regex is rejected at save time, not at run time

## Documentation

Update `README.md` in the same change as the code. The environment variable
table, the endpoint table and the match-type table are load-bearing — someone
deploying this reads those and nothing else.
