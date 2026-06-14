"""FastAPI app: scan, search (TMDB), rip, jobs, SSE progress."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from .dvd import scan
from .models import (
    ContentType,
    Job,
    RipRequest,
    ScanResult,
    TMDBEpisode,
    TMDBMovieResult,
    TMDBTVResult,
)
from .ripper import Ripper
from .tmdb import TMDBClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

DVD_DEVICE = os.getenv("DVD_DEVICE", "/dev/sr0")
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "/output"))

# Per-process token appended to static asset URLs so each container start busts
# any browser cache. New container → new token → forced refetch of JS/CSS.
ASSET_VERSION = secrets.token_hex(6)

BASE_DIR = Path(__file__).parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    ripper = Ripper(device=DVD_DEVICE, output_root=OUTPUT_ROOT)
    await ripper.start()
    tmdb = TMDBClient()
    app.state.ripper = ripper
    app.state.tmdb = tmdb
    try:
        yield
    finally:
        await ripper.stop()
        await tmdb.aclose()


app = FastAPI(title="DVD Rip", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


@app.get("/api/scan", response_model=ScanResult)
async def api_scan() -> ScanResult:
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
            movie=payload.movie,
            tv=payload.tv,
        )
        job_ids.append(job.id)
    return {"job_ids": job_ids}


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


