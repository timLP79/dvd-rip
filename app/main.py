"""FastAPI app: scan, search (TMDB), rip, jobs, SSE progress."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import cd, hostenv
from .bluray import detect_and_scan as bluray_detect_and_scan
from .dvd import is_dvd_video, scan
from .library import Library
from .models import (
    ContentType,
    CDRipRequest,
    EncoderMode,
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
from .musicbrainz import MusicBrainzClient
from .push import PushSender
from .ripper import Ripper
from .scan_coordinator import ScanBusyError, ScanCoordinator
from .tmdb import TMDBClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# Storage defaults differ per host: the container's /output and /data are bind-mount
# targets that don't exist when running natively on macOS. See hostenv.
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", hostenv.default_output_root()))
LIBRARY_DB = os.getenv("LIBRARY_DB", hostenv.default_library_db())
MAKEMKV_SCRATCH = os.getenv("MAKEMKV_SCRATCH", hostenv.default_scratch_dir())

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
    logging.getLogger(__name__).info("host configuration: %s", hostenv.describe())
    ripper = Ripper(device=await hostenv.resolve_optical_device(),
                    output_root=OUTPUT_ROOT, library=library,
                    scratch_dir=MAKEMKV_SCRATCH, push=push,
                    device_provider=hostenv.resolve_optical_device)
    await ripper.start()
    tmdb = TMDBClient()
    musicbrainz = MusicBrainzClient()
    app.state.ripper = ripper
    app.state.tmdb = tmdb
    app.state.musicbrainz = musicbrainz
    app.state.library = library
    app.state.push = push
    app.state.scan_coordinator = ScanCoordinator()
    try:
        yield
    finally:
        await ripper.stop()
        await tmdb.aclose()
        await musicbrainz.aclose()


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
            # Resolved per request, not cached at startup: on macOS the drive is
            # detected live, so the header reflects a drive plugged in just now.
            "device": await hostenv.device_label(),
            "output_root": str(OUTPUT_ROOT),
            "tmdb_enabled": tmdb.enabled,
            "speed_available": hostenv.speed_mode_available(),
            "asset_version": ASSET_VERSION,
        },
    )


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root so its scope is "/" (covers the whole app), not "/static/".
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "library.html", {"asset_version": ASSET_VERSION}
    )


@app.get("/api/scan", response_model=ScanResult)
async def api_scan(request: Request) -> ScanResult:
    # A running rip owns the optical drive; refuse to touch it mid-rip.
    ripper: Ripper = request.app.state.ripper
    if ripper.is_busy():
        raise HTTPException(status_code=409, detail="Rip in progress; disc scan unavailable")
    coordinator: ScanCoordinator = request.app.state.scan_coordinator

    async def _probe(register_proc) -> ScanResult:
        # Resolved once per scan so every probe below addresses the same drive,
        # and so a drive attached since the last scan is picked up (macOS).
        device = await hostenv.resolve_optical_device()
        # libdiscid names drives by an OS-defined id, which on macOS is a drive
        # index rather than a path — translate, or read() fails and the disc gets
        # misclassified as "not an audio CD" and falls through to the DVD scanner.
        cd_device = hostenv.discid_device(device)
        # Audio CD is the cheapest, most specific probe (libdiscid, ~1s); it is
        # an in-process call, so it is not subprocess-cancelable (see dvd-rip-r6n).
        if await cd.is_audio_cd(cd_device):
            return await cd.scan(cd_device, mb_client=request.app.state.musicbrainz)
        if coordinator.cancelled:
            return ScanResult(dvd_label="UNKNOWN_DVD", titles=[])
        if await is_dvd_video(device, register_proc=register_proc):
            return await scan(device, register_proc=register_proc)
        if coordinator.cancelled:
            return ScanResult(dvd_label="UNKNOWN_DVD", titles=[])
        bd = await bluray_detect_and_scan(device, register_proc=register_proc)
        if bd is not None:
            return bd
        if coordinator.cancelled:
            return ScanResult(dvd_label="UNKNOWN_DVD", titles=[])
        return await scan(device, register_proc=register_proc)

    try:
        return await coordinator.run(_probe)
    except ScanBusyError:
        raise HTTPException(status_code=409, detail="Scan already in progress")


@app.post("/api/scan/cancel")
async def api_scan_cancel(request: Request) -> dict:
    coordinator: ScanCoordinator = request.app.state.scan_coordinator
    cancelled = await coordinator.cancel()
    return {"cancelled": cancelled}


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


def _require_scan_idle(request: Request) -> None:
    """Route dependency: reject a rip while a scan owns the drive.

    Declared as a `Depends`, not an inline check in the handler body: FastAPI
    resolves declared dependencies before it validates/parses the JSON request
    body, so this fires (and returns 409) even for a malformed payload that
    would otherwise 422 before the handler's first line ever runs.
    """
    coordinator: ScanCoordinator = request.app.state.scan_coordinator
    if coordinator.busy:
        raise HTTPException(status_code=409, detail="Scan in progress; wait for it to finish or cancel it")


@app.post("/api/rip", dependencies=[Depends(_require_scan_idle)])
async def api_rip(payload: RipRequest, request: Request) -> dict:
    if not payload.titles:
        raise HTTPException(status_code=400, detail="No titles selected")
    # Reject rather than quietly falling back to x264: the user picked Speed for
    # the speed, and a silent substitution just looks like the rip hung.
    if payload.encoder_mode is EncoderMode.SPEED and not hostenv.speed_mode_available():
        raise HTTPException(
            status_code=400,
            detail="Speed mode is unavailable on this host (no supported hardware "
                   "encoder); use Quality mode.",
        )
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


@app.post("/api/cd/rip", dependencies=[Depends(_require_scan_idle)])
async def api_cd_rip(payload: CDRipRequest, request: Request) -> dict:
    if not any(t.include for t in payload.tracks):
        raise HTTPException(status_code=400, detail="No tracks selected")
    if not payload.creator.strip() or not payload.title.strip():
        raise HTTPException(status_code=400, detail="creator and title are required")
    ripper: Ripper = request.app.state.ripper
    job = ripper.enqueue_cd(request=payload)
    return {"job_ids": [job.id]}


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


