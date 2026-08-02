# Inoreader Dynamic Tagging

Automatically apply tags to your Inoreader articles based on URL patterns.

Runs two ways:

- **As a service** — a scheduled, multi-account web service with a status page
  showing whether recent runs succeeded and which accounts need reconnecting.
- **As a one-shot CLI** — the original single-account, `config.json`,
  cron-it-yourself workflow.

## Features

- 🏷️ **Dynamic tagging** from URL patterns — domain, path, full URL, or regex
- 👥 **Multiple accounts**, each with its own rules, schedule and state
- ⏱️ **Built-in scheduler** — no cron entry needed
- 📊 **Status page** — recent runs per account, success/failure, and an obvious
  badge when an Inoreader connection has expired
- 💾 **Persistent backend** — SQLite (or any SQLAlchemy URL), so state survives
  restarts
- 🔐 **Refresh tokens encrypted at rest**
- 🧪 **Dry-run mode** per account

---

## Running as a service

### Quick start with Docker Compose

Register an application at the
[Inoreader Developer Portal](https://www.inoreader.com/developers/) and set its
redirect URI to `http://localhost:8000/auth/callback`.

```bash
export INOREADER_APP_ID=your_app_id
export INOREADER_APP_KEY=your_app_key
docker compose up -d
```

Open <http://localhost:8000> and click **Connect an Inoreader account**.

The account is created from its Inoreader identity — there are no separate
passwords to manage. It gets a starter rule set and begins running on a
schedule; edit the rules and interval from its page.

### Configuration

All configuration is environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `INOREADER_APP_ID` | — | OAuth application ID (required) |
| `INOREADER_APP_KEY` | — | OAuth application secret (required) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | External URL; the OAuth redirect is this + `/auth/callback` |
| `DATA_DIR` | `/data` | Where the database and generated encryption key live |
| `DATABASE_URL` | SQLite in `DATA_DIR` | Any SQLAlchemy URL |
| `SQLITE_JOURNAL_MODE` | `TRUNCATE` | Use `WAL` on a local disk; `TRUNCATE` on NFS (see below) |
| `ENCRYPTION_KEY` | generated into `DATA_DIR` | Fernet key protecting stored refresh tokens |
| `DEFAULT_INTERVAL_MINUTES` | `30` | Schedule given to a newly connected account |
| `DEFAULT_MAX_ARTICLES` | `200` | Article ceiling per run for a new account |
| `DEFAULT_BATCH_SIZE` | `100` | Articles fetched per API call |
| `RUN_HISTORY_LIMIT` | `50` | Run records retained per account |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `8000` | Bind address |
| `LOG_LEVEL` | `INFO` | |

`PUBLIC_BASE_URL` must match the redirect URI registered with Inoreader
exactly, or authorization fails.

### Endpoints

| Path | Purpose |
|---|---|
| `/` | Status page |
| `/users/{id}` | Account settings, rules, run history |
| `/runs/{id}` | Full log for one run |
| `/api/status` | JSON status — `healthy` is `false` when an account needs reconnecting |
| `/healthz` | Liveness: process up and scheduler thread alive |
| `/readyz` | Readiness: database reachable |

Monitoring hook:

```bash
curl -s http://localhost:8000/api/status | jq '.healthy, .needs_reauth'
```

### Security model

**There is no login.** Anyone who can reach the page can see every connected
account, change its rules, trigger runs, and remove it. This is a deliberate
choice for a service on a trusted home network, and it is the wrong default for
anything else.

Refresh tokens are encrypted at rest with `ENCRYPTION_KEY`, so a leaked database
file alone does not hand over anyone's reading account. That is the only
protection there is — put the service behind an authenticating proxy before
exposing it more widely.

### Storage

SQLite is the default and is enough for this workload: a single writer doing a
handful of transactions per run.

WAL mode requires shared memory that NFS does not provide, so `TRUNCATE` is the
default journal mode. On a local disk, set `SQLITE_JOURNAL_MODE=WAL` for better
concurrency.

**Run exactly one instance.** Two would mean two schedulers double-tagging the
same articles and two writers on one SQLite file. If you need more, point
`DATABASE_URL` at Postgres — but the scheduler is still single-instance by
design.

---

## Running as a CLI

The original workflow, unchanged in behaviour:

```bash
pip install -r requirements.txt
cp config.example.json config.json   # then fill in app_id, app_key, refresh_token
python -m inoreader_tagger run --dry-run
python -m inoreader_tagger run
```

| Option | Purpose |
|---|---|
| `--config` | Configuration file path (default `config.json`) |
| `--dry-run` | Match rules but never apply tags |
| `--max-articles` | Article ceiling for the run (default 200) |
| `--batch-size` | Articles fetched per API call (default 100) |
| `--force-timestamp-update` | Advance the high-water mark even when the ceiling was hit |
| `--no-timestamp-tracking` | Reconsider all unread articles |
| `--reset-timestamp` | Clear the high-water mark and exit |
| `--timestamp-file` | Where the mark is stored |
| `--verbose` | Print the full run log |

Cron:

```
0 * * * * cd /path/to/inoreader-tagger && python -m inoreader_tagger run
```

The CLI cannot complete an OAuth flow on its own. Either connect the account
once through the service and copy the refresh token out, or paste in a token you
already have.

---

## Tagging rules

A JSON array. Each rule needs a `pattern`, a `match_type`, and a `tags` array.

```json
{
  "pattern": "github.com",
  "match_type": "domain",
  "tags": ["GitHub", "Development"],
  "description": "Optional"
}
```

### Match types

| Type | Matches against | Example |
|---|---|---|
| `domain` | The host portion | `github.com` matches `gist.github.com` |
| `path` | The path portion | `/blog/` matches `example.com/blog/post` |
| `full` | Anywhere in the URL | `python` matches `example.com/python-tutorial` |
| `regex` | Regex over the full URL | `youtube\.com\|youtu\.be` |

All matching is case-insensitive. A URL can match several rules and collect tags
from each; duplicates are removed.

### Capture groups

With `match_type: "regex"`, `{0}` expands to the whole match and `{1}`, `{2}`…
to capture groups:

```json
{
  "pattern": "reddit\\.com/r/([^/]+)",
  "match_type": "regex",
  "tags": ["Reddit", "r/{1}"]
}
```

`https://reddit.com/r/programming/comments/xyz` → `Reddit`, `r/programming`.

More examples:

```json
[
  {
    "pattern": "github\\.com/([^/]+)/([^/]+)",
    "match_type": "regex",
    "tags": ["GitHub", "{1}/{2}", "User:{1}"]
  },
  {
    "pattern": "youtube\\.com/@([^/?]+)",
    "match_type": "regex",
    "tags": ["YouTube", "Channel:{1}"]
  }
]
```

Rules are validated when saved from the status page — a bad regex or an unknown
`match_type` is rejected rather than failing silently at the next run.

---

## How a run works

1. Refresh the access token. If Inoreader rejects the refresh token, the run is
   recorded as **Re-login needed** and the account is flagged on the status page.
2. Fetch unread articles newer than the account's high-water mark. Read articles
   are excluded server-side and are never touched.
3. Match each article's URL against the rules; skip tags it already has.
4. Apply tags in batches — one API call per distinct tag.
5. Advance the high-water mark.

### About the high-water mark

The mark is the timestamp passed to Inoreader on the next run, so anything older
is never looked at again. It only advances to the newest article processed
**with no errors**, and does not advance at all when:

- the run was a dry run,
- every article errored,
- or the run hit its article ceiling — meaning older unread articles may remain,
  so advancing would skip them permanently.

That last case is why hitting `--max-articles` leaves the mark alone: run again
and it picks up where it left off. `--force-timestamp-update` overrides this and
can skip articles.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Layout:

| Module | Responsibility |
|---|---|
| `api.py` | Inoreader HTTP client and OAuth |
| `matcher.py` | URL → tags, and rule validation |
| `tagger.py` | The run engine; owns high-water mark logic |
| `runner.py` | Runs the engine and records the outcome |
| `scheduler.py` | Per-account recurring jobs |
| `web.py` | Status page, OAuth, settings |
| `db.py` | Models and engine |
| `cli.py` | Single-account command line |

`migrate_tags.py` is a separate one-off tool for reshaping existing tags; it
still uses `config.json`.

---

## Upgrading from 1.x

`inoreader_tagger.py` is now the package `inoreader_tagger/`, so
`python inoreader_tagger.py` becomes `python -m inoreader_tagger run`.
`from inoreader_tagger import InoreaderAPI, URLPatternMatcher` still works.

Existing `config.json` files and `.last_processed_timestamp` work unchanged with
the CLI. The service does not read them — connect the account through the web UI
and it starts with fresh state.

---

## License

MIT.
