"""FastAPI application: status page, self-service Inoreader signup, settings.

There is no login. The service is meant to sit on a trusted LAN, and anyone who
can reach it can see every connected account and trigger runs. That is a
deliberate deployment choice — see README "Security model" before exposing it
anywhere wider.
"""

import datetime as dt
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .api import InoreaderAPI, InoreaderError
from .config import load_settings
from .crypto import TokenCipher
from .db import (
    STATUS_AUTH_REQUIRED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    Database,
    Run,
    User,
    utcnow,
)
from .defaults import DEFAULT_RULES
from .matcher import validate_rules
from .runner import RunService
from .scheduler import TaggerScheduler

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

OAUTH_STATE_KEY = "oauth_state"
FLASH_KEY = "flash"


def create_app() -> FastAPI:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    database = Database(settings)
    database.create_all()

    cipher = TokenCipher.from_settings(settings)
    run_service = RunService(settings, database, cipher)
    scheduler = TaggerScheduler(settings, database, run_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(
        title="Inoreader Tagger",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # The session cookie only carries the OAuth state nonce and flash
    # messages, but it still needs a stable secret across restarts or an
    # in-flight authorization would fail after a redeploy.
    app.add_middleware(
        SessionMiddleware,
        secret_key=cipher.derive_secret("session-signing"),
        same_site="lax",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["humantime"] = _humantime
    templates.env.filters["duration"] = _duration

    app.state.settings = settings
    app.state.database = database
    app.state.cipher = cipher
    app.state.run_service = run_service
    app.state.scheduler = scheduler

    # -- Status page ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        session = database.session()
        try:
            users = session.scalars(select(User).order_by(User.created_at)).all()
            cards = [_user_card(user, scheduler, run_service) for user in users]
        finally:
            session.close()

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "cards": cards,
                "settings": settings,
                "flash": _take_flash(request),
                "attention": [c for c in cards if c["needs_attention"]],
                "now": utcnow(),
            },
        )

    @app.get("/users/{user_id}", response_class=HTMLResponse)
    def user_detail(request: Request, user_id: int):
        session = database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="No such user")

            card = _user_card(user, scheduler, run_service, run_limit=25)
            rules_text = json.dumps(user.rules, indent=2)
        finally:
            session.close()

        return templates.TemplateResponse(
            request,
            "user.html",
            {
                "card": card,
                "rules_text": rules_text,
                "settings": settings,
                "flash": _take_flash(request),
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        session = database.session()
        try:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="No such run")
            view = _run_view(run)
            user_label = run.user.label
            user_id = run.user_id
        finally:
            session.close()

        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "run": view,
                "user_label": user_label,
                "user_id": user_id,
                "settings": settings,
            },
        )

    # -- OAuth ---------------------------------------------------------------

    @app.get("/auth/login")
    def auth_login(request: Request):
        if not settings.oauth_configured:
            _flash(request, "error", "Server is missing INOREADER_APP_ID / INOREADER_APP_KEY.")
            return RedirectResponse("/", status_code=303)

        api = InoreaderAPI(
            app_id=settings.app_id,
            app_key=settings.app_key,
            redirect_uri=settings.redirect_uri,
        )
        url, state = api.get_authorization_url(secrets.token_urlsafe(32))
        request.session[OAUTH_STATE_KEY] = state
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/callback")
    def auth_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        if error:
            _flash(request, "error", f"Inoreader refused the authorization: {error}")
            return RedirectResponse("/", status_code=303)

        expected_state = request.session.pop(OAUTH_STATE_KEY, None)
        if not state or not expected_state or not secrets.compare_digest(state, expected_state):
            _flash(request, "error", "Authorization state mismatch — please try connecting again.")
            return RedirectResponse("/", status_code=303)

        if not code:
            _flash(request, "error", "Inoreader did not return an authorization code.")
            return RedirectResponse("/", status_code=303)

        api = InoreaderAPI(
            app_id=settings.app_id,
            app_key=settings.app_key,
            redirect_uri=settings.redirect_uri,
        )

        try:
            api.exchange_code_for_token(code)
            info = api.get_user_info()
        except InoreaderError as exc:
            logger.warning("OAuth exchange failed: %s", exc)
            _flash(request, "error", f"Could not complete the connection: {exc}")
            return RedirectResponse("/", status_code=303)

        inoreader_user_id = str(info.get("userId") or "").strip()
        if not inoreader_user_id:
            _flash(request, "error", "Inoreader did not identify the account.")
            return RedirectResponse("/", status_code=303)

        session = database.session()
        try:
            user = session.scalar(
                select(User).where(User.inoreader_user_id == inoreader_user_id)
            )
            reconnected = user is not None

            if user is None:
                user = User(
                    inoreader_user_id=inoreader_user_id,
                    interval_minutes=settings.default_interval_minutes,
                    max_articles=settings.default_max_articles,
                    batch_size=settings.default_batch_size,
                )
                user.rules = DEFAULT_RULES
                session.add(user)

            user.email = info.get("userEmail") or user.email
            user.display_name = info.get("userName") or user.display_name
            user.refresh_token_encrypted = cipher.encrypt(api.refresh_token)
            user.needs_reauth = False
            user.last_authenticated_at = utcnow()
            session.commit()
            user_id = user.id
            label = user.label
        finally:
            session.close()

        scheduler.reconcile()

        if reconnected:
            _flash(request, "success", f"Reconnected {label}.")
        else:
            _flash(
                request,
                "success",
                f"Connected {label}. Starter tagging rules were added — edit them below.",
            )
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    # -- Actions -------------------------------------------------------------

    @app.post("/users/{user_id}/run")
    def trigger_run(request: Request, user_id: int):
        session = database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="No such user")
            label = user.label
            connected = bool(user.refresh_token_encrypted)
        finally:
            session.close()

        if not connected:
            _flash(request, "error", f"{label} has no Inoreader connection yet.")
        elif run_service.is_running(user_id):
            _flash(request, "error", f"A run for {label} is already in progress.")
        else:
            scheduler.trigger_now(user_id)
            _flash(request, "success", f"Run queued for {label}.")

        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/settings")
    def update_settings(
        request: Request,
        user_id: int,
        interval_minutes: int = Form(...),
        max_articles: int = Form(...),
        batch_size: int = Form(...),
        folder_filter: str = Form(""),
        enabled: str = Form("off"),
        dry_run: str = Form("off"),
    ):
        problems = []
        if interval_minutes < 1:
            problems.append("Interval must be at least 1 minute.")
        if max_articles < 1:
            problems.append("Max articles must be at least 1.")
        if batch_size < 1:
            problems.append("Batch size must be at least 1.")
        if batch_size > max_articles:
            problems.append("Batch size cannot exceed max articles.")

        if problems:
            _flash(request, "error", " ".join(problems))
            return RedirectResponse(f"/users/{user_id}", status_code=303)

        session = database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="No such user")

            user.interval_minutes = interval_minutes
            user.max_articles = max_articles
            user.batch_size = batch_size
            user.folder_filter = folder_filter.strip() or None
            user.enabled = enabled == "on"
            user.dry_run = dry_run == "on"
            session.commit()
        finally:
            session.close()

        scheduler.reconcile()
        _flash(request, "success", "Settings saved.")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/rules")
    def update_rules(request: Request, user_id: int, rules: str = Form(...)):
        try:
            parsed = json.loads(rules)
        except json.JSONDecodeError as exc:
            _flash(request, "error", f"Rules are not valid JSON: {exc}")
            return RedirectResponse(f"/users/{user_id}", status_code=303)

        problems = validate_rules(parsed)
        if problems:
            _flash(request, "error", " ".join(problems))
            return RedirectResponse(f"/users/{user_id}", status_code=303)

        session = database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="No such user")
            user.rules = parsed
            session.commit()
        finally:
            session.close()

        _flash(request, "success", f"Saved {len(parsed)} rule(s).")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/reset-timestamp")
    def reset_timestamp(request: Request, user_id: int):
        session = database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="No such user")
            user.last_processed_timestamp = None
            session.commit()
        finally:
            session.close()

        _flash(
            request,
            "success",
            "High-water mark cleared. The next run will reconsider recent unread articles.",
        )
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/delete")
    def delete_user(request: Request, user_id: int, confirm: str = Form("")):
        session = database.session()
        try:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="No such user")
            label = user.label

            if confirm.strip() != (user.email or user.inoreader_user_id):
                _flash(request, "error", "Confirmation text did not match — nothing was deleted.")
                return RedirectResponse(f"/users/{user_id}", status_code=303)

            session.delete(user)
            session.commit()
        finally:
            session.close()

        scheduler.reconcile()
        _flash(request, "success", f"Removed {label} and its run history.")
        return RedirectResponse("/", status_code=303)

    # -- Machine-readable ----------------------------------------------------

    @app.get("/api/status")
    def api_status():
        session = database.session()
        try:
            users = session.scalars(select(User).order_by(User.created_at)).all()
            payload = []
            for user in users:
                last = user.runs[0] if user.runs else None
                next_run = scheduler.next_run_time(user.id)
                payload.append(
                    {
                        "id": user.id,
                        "label": user.label,
                        "enabled": user.enabled,
                        "needs_reauth": user.needs_reauth,
                        "interval_minutes": user.interval_minutes,
                        "next_run": _iso(next_run),
                        "last_run": None
                        if last is None
                        else {
                            "id": last.id,
                            "status": last.status,
                            "started_at": _iso(last.started_at),
                            "finished_at": _iso(last.finished_at),
                            "processed": last.processed,
                            "tagged": last.tagged,
                            "errors": last.errors,
                            "error_message": last.error_message,
                        },
                    }
                )
        finally:
            session.close()

        needs_attention = [u["label"] for u in payload if u["needs_reauth"]]
        return JSONResponse(
            {
                "users": payload,
                "needs_reauth": needs_attention,
                "healthy": not needs_attention,
            }
        )

    @app.get("/healthz")
    def healthz():
        """Liveness: the process is up and the scheduler thread is alive."""
        if not scheduler.running:
            return JSONResponse({"status": "scheduler stopped"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    def readyz():
        """Readiness: the database is reachable and writable."""
        session = database.session()
        try:
            session.execute(select(User).limit(1))
            return JSONResponse({"status": "ok"})
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            logger.warning("Readiness probe failed: %s", exc)
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        finally:
            session.close()

    return app


# -- View helpers -----------------------------------------------------------


def _user_card(user: User, scheduler, run_service, run_limit: int = 5) -> dict:
    """Flatten a User into template-ready data. Requires an open DB session,
    because run history is loaded lazily."""
    runs = user.runs[:run_limit]
    last = runs[0] if runs else None
    next_run = scheduler.next_run_time(user.id)

    return {
        "id": user.id,
        "label": user.label,
        "email": user.email,
        "display_name": user.display_name,
        "enabled": user.enabled,
        "dry_run": user.dry_run,
        "needs_reauth": user.needs_reauth,
        "connected": bool(user.refresh_token_encrypted),
        "interval_minutes": user.interval_minutes,
        "folder_filter": user.folder_filter,
        "max_articles": user.max_articles,
        "batch_size": user.batch_size,
        "rule_count": len(user.rules),
        "last_processed_timestamp": user.last_processed_timestamp,
        "last_authenticated_at": user.last_authenticated_at,
        "next_run": next_run,
        "is_running": run_service.is_running(user.id),
        "last_run": _run_view(last) if last else None,
        "runs": [_run_view(run) for run in runs],
        "needs_attention": user.needs_reauth
        or (last is not None and last.status in (STATUS_FAILED, STATUS_AUTH_REQUIRED)),
    }


_STATUS_LABELS = {
    STATUS_SUCCESS: ("ok", "Success"),
    STATUS_PARTIAL: ("warn", "Partial"),
    STATUS_FAILED: ("bad", "Failed"),
    STATUS_AUTH_REQUIRED: ("bad", "Re-login needed"),
}


def _run_view(run: Run) -> dict:
    tone, label = _STATUS_LABELS.get(run.status, ("neutral", "Running"))
    return {
        "id": run.id,
        "status": run.status,
        "tone": tone,
        "label": label,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": run.duration_seconds,
        "processed": run.processed,
        "tagged": run.tagged,
        "skipped": run.skipped,
        "errors": run.errors,
        "error_message": run.error_message,
        "triggered_manually": run.triggered_manually,
        "log": run.log,
    }


def _iso(value) -> Optional[str]:
    """ISO-8601 with an explicit zone.

    SQLite has no native timestamp type, so values come back naive even though
    the column is declared timezone-aware. Everything is written as UTC, so
    that is what a bare value is labelled as.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def _flash(request: Request, tone: str, message: str) -> None:
    request.session[FLASH_KEY] = {"tone": tone, "message": message}


def _take_flash(request: Request) -> Optional[dict]:
    return request.session.pop(FLASH_KEY, None)


def _humantime(value) -> str:
    """Render a timestamp as a compact relative age."""
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)

    delta = (utcnow() - value).total_seconds()
    future = delta < 0
    delta = abs(delta)

    if delta < 60:
        return "in <1m" if future else "just now"

    for cutoff, unit, suffix in (
        (3600, 60, "m"),
        (86400, 3600, "h"),
        (604800, 86400, "d"),
    ):
        if delta < cutoff:
            amount = int(delta // unit)
            break
    else:
        amount, suffix = int(delta // 604800), "w"

    return f"in {amount}{suffix}" if future else f"{amount}{suffix} ago"


def _duration(seconds) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"
