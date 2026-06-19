"""FastAPI app: scan, search (TMDB), rip, jobs, SSE progress."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from .bluray import detect_and_scan as bluray_detect_and_scan
from .dvd import is_dvd_video, scan
from .library import Library
from .models import (
    ContentType,
    Job,
    PushSubscription,
    PushUnsubscribe,
    RipRecord,
    RipRequest,
    ScanResult,
    TMDBEpisode,
    TMDBMovieResult,
    TMDBTVResult,
)
from .push import PushSender
from .ripper import Ripper
from .tmdb import TMDBClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

DVD_DEVICE = os.getenv("DVD_DEVICE", "/dev/sr0")
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "/output"))
LIBRARY_DB = os.getenv("LIBRARY_DB", "/data/library.db")
MAKEMKV_SCRATCH = os.getenv("MAKEMKV_SCRATCH", "/data/scratch")

# Per-process token appended to static asset URLs so each container start busts
# any browser cache. New container → new token → forced refetch of JS/CSS.
ASSET_VERSION = secrets.token_hex(6)

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)

# Web Push endpoints must point at a known browser push service. The server
# POSTs to sub.endpoint via pywebpush, so an arbitrary endpoint is an SSRF
# vector; restrict to the major services and cap how many we store.
_ALLOWED_PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)
MAX_PUSH_SUBSCRIPTIONS = int(os.getenv("MAX_PUSH_SUBSCRIPTIONS", "50"))


def _is_allowed_push_endpoint(endpoint: str) -> bool:
    try:
        u = urlparse(endpoint)
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_PUSH_HOST_SUFFIXES)
SESSION_SECRET = os.getenv("SESSION_SECRET", "") or secrets.token_hex(32)
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
AUTH_EXEMPT = {"/login", "/sw.js"}

BASE_DIR = Path(__file__).parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    library = Library(LIBRARY_DB)
    library.init_db()
    try:
        library.backfill(str(OUTPUT_ROOT))
    except Exception:
        logging.getLogger(__name__).exception("library backfill failed")
    push = PushSender(library)
    ripper = Ripper(device=DVD_DEVICE, output_root=OUTPUT_ROOT, library=library,
                    scratch_dir=MAKEMKV_SCRATCH, push=push)
    await ripper.start()
    tmdb = TMDBClient()
    app.state.ripper = ripper
    app.state.tmdb = tmdb
    app.state.library = library
    app.state.push = push
    try:
        yield
    finally:
        await ripper.stop()
        await tmdb.aclose()


app = FastAPI(title="DVD Rip", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)
    path = request.url.path
    if path in AUTH_EXEMPT or path.startswith("/static/"):
        return await call_next(request)
    if request.session.get("authed"):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=COOKIE_SECURE,
    same_site="lax",
    max_age=60 * 60 * 24 * 60,  # 60 days
)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"asset_version": ASSET_VERSION, "error": None}
    )


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    if AUTH_ENABLED and secrets.compare_digest(password, APP_PASSWORD):
        request.session["authed"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"asset_version": ASSET_VERSION, "error": "Incorrect password"},
        status_code=401,
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    tmdb: TMDBClient = request.app.state.tmdb
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "device": DVD_DEVICE,
            "output_root": str(OUTPUT_ROOT),
            "tmdb_enabled": tmdb.enabled,
            "asset_version": ASSET_VERSION,
        },
    )


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root so its scope is "/" (covers the whole app), not "/static/".
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "library.html", {"asset_version": ASSET_VERSION}
    )


@app.get("/api/scan", response_model=ScanResult)
async def api_scan() -> ScanResult:
    # Route DVDs straight to the DVD scanner: a quick lsdvd probe avoids running
    # makemkvcon (the Blu-ray detector) on a disc it would never rip. Only non-DVD
    # discs go to makemkvcon-based Blu-ray detection.
    if await is_dvd_video(DVD_DEVICE):
        return await scan(DVD_DEVICE)
    bd = await bluray_detect_and_scan(DVD_DEVICE)
    if bd is not None:
        return bd
    return await scan(DVD_DEVICE)


@app.get("/api/search")
async def api_search(
    request: Request,
    type: Literal["movie", "tv"] = Query(..., description="movie or tv"),
    query: str = Query(..., min_length=1),
) -> dict:
    tmdb: TMDBClient = request.app.state.tmdb
    if type == "movie":
        results: list[TMDBMovieResult] | list[TMDBTVResult] = await tmdb.search_movie(query)
    else:
        results = await tmdb.search_tv(query)
    return {
        "tmdb_enabled": tmdb.enabled,
        "results": [r.model_dump() for r in results],
    }


@app.get("/api/tv/{tv_id}/seasons/{season}/episodes", response_model=list[TMDBEpisode])
async def api_tv_episodes(tv_id: int, season: int, request: Request) -> list[TMDBEpisode]:
    tmdb: TMDBClient = request.app.state.tmdb
    return await tmdb.get_tv_season_episodes(tv_id, season)


@app.post("/api/rip")
async def api_rip(payload: RipRequest, request: Request) -> dict:
    if not payload.titles:
        raise HTTPException(status_code=400, detail="No titles selected")
    if payload.content_type is ContentType.MOVIE and payload.movie is None:
        raise HTTPException(status_code=400, detail="movie metadata required for content_type=movie")
    if payload.content_type is ContentType.TV:
        if payload.tv is None:
            raise HTTPException(status_code=400, detail="tv metadata required for content_type=tv")
        missing = [t.index for t in payload.titles if t.episode_number is None]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"episode_number required for TV titles (missing on: {missing})",
            )

    ripper: Ripper = request.app.state.ripper
    job_ids: list[str] = []
    for selection in payload.titles:
        job = ripper.enqueue(
            selection=selection,
            encoder_mode=payload.encoder_mode,
            content_type=payload.content_type,
            burn_forced=payload.burn_forced_subtitles,
            disc_format=payload.disc_format,
            movie=payload.movie,
            tv=payload.tv,
        )
        job_ids.append(job.id)
    return {"job_ids": job_ids}


@app.get("/api/library", response_model=list[RipRecord])
async def api_library(request: Request, include_unsuccessful: bool = False) -> list[RipRecord]:
    library: Library = request.app.state.library
    return library.list_rips(include_unsuccessful=include_unsuccessful)


@app.get("/api/library/check")
async def api_library_check(
    request: Request,
    content_type: Literal["movie", "tv"] = Query(...),
    tmdb_id: Optional[int] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> dict:
    library: Library = request.app.state.library
    matches = library.find_existing(
        content_type=content_type, tmdb_id=tmdb_id, title=title,
        year=year, season=season, episode=episode,
    )
    return {"matches": [m.model_dump() for m in matches]}


@app.get("/api/jobs", response_model=list[Job])
async def api_jobs(request: Request) -> list[Job]:
    ripper: Ripper = request.app.state.ripper
    return ripper.list_jobs()


@app.get("/api/jobs/{job_id}", response_model=Job)
async def api_job(job_id: str, request: Request) -> Job:
    ripper: Ripper = request.app.state.ripper
    job = ripper.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs/{job_id}/progress")
async def api_job_progress(job_id: str, request: Request):
    ripper: Ripper = request.app.state.ripper
    if not ripper.get(job_id):
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        async for update in ripper.subscribe(job_id):
            if await request.is_disconnected():
                break
            yield {"event": "progress", "data": update.model_dump_json()}

    return EventSourceResponse(event_stream())


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str, request: Request) -> dict:
    ripper: Ripper = request.app.state.ripper
    ok = await ripper.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job not cancellable")
    return {"cancelled": True}


@app.get("/api/push/key")
async def api_push_key(request: Request) -> dict:
    push: PushSender = request.app.state.push
    return {"enabled": push.enabled, "key": push.public_key}


@app.post("/api/push/subscribe")
async def api_push_subscribe(sub: PushSubscription, request: Request) -> dict:
    if not _is_allowed_push_endpoint(sub.endpoint):
        raise HTTPException(status_code=400, detail="Unsupported push endpoint")
    library: Library = request.app.state.library
    if len(library.list_subscriptions()) >= MAX_PUSH_SUBSCRIPTIONS:
        raise HTTPException(status_code=429, detail="Too many push subscriptions")
    library.add_subscription(sub.endpoint, sub.keys.p256dh, sub.keys.auth)
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(body: PushUnsubscribe, request: Request) -> dict:
    library: Library = request.app.state.library
    library.remove_subscription(body.endpoint)
    return {"ok": True}


@app.post("/api/push/test")
async def api_push_test(request: Request) -> dict:
    push: PushSender = request.app.state.push
    sent = await push.notify_test()
    return {"sent": sent}


