"""FastAPI dashboard: annotated MJPEG preview, SSE alerts, and a small REST API."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import FastAPI
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .pipeline import Pipeline

log = logging.getLogger("scout.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_BOUNDARY = "frame"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline = Pipeline(settings)
        pipeline.broker.attach_loop(asyncio.get_running_loop())
        pipeline.start()
        app.state.pipeline = pipeline
        log.info("dashboard ready at http://%s:%s", settings.server.host, settings.server.port)
        try:
            yield
        finally:
            pipeline.stop()

    app = FastAPI(title="EyePop Scout Monitor", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        pipeline: Pipeline = app.state.pipeline
        period = 1.0 / max(1.0, settings.camera.stream_fps)

        async def frames():
            while True:
                frame = await asyncio.to_thread(pipeline.annotate_latest)
                if frame is None:
                    await asyncio.sleep(0.2)
                    continue
                ok, buf = await asyncio.to_thread(cv2.imencode, ".jpg", frame)
                if ok:
                    data = buf.tobytes()
                    yield (
                        f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(data)}\r\n\r\n"
                    ).encode() + data + b"\r\n"
                await asyncio.sleep(period)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        )

    @app.get("/events")
    async def events():
        pipeline: Pipeline = app.state.pipeline
        queue = pipeline.broker.subscribe()

        async def gen():
            # Prime the client with the current full state.
            yield f"data: {json.dumps({'type': 'state', 'state': pipeline.state()})}\n\n"
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # comment frame keeps proxies happy
            finally:
                pipeline.broker.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        return JSONResponse(app.state.pipeline.state())

    @app.get("/api/config")
    async def api_config() -> JSONResponse:
        data = settings.model_dump()
        data["eyepop"]["secret_key"] = bool(settings.eyepop.secret_key)  # never leak the key
        return JSONResponse(data)

    @app.post("/api/doors/calibrate")
    async def calibrate() -> JSONResponse:
        pipeline: Pipeline = app.state.pipeline
        path = settings.doors.reference_image or "data/door_reference.jpg"
        ok = await asyncio.to_thread(pipeline.recalibrate_doors, path)
        status = 200 if ok else 409
        msg = "reference captured" if ok else "no frame available yet"
        return JSONResponse({"ok": ok, "message": msg, "path": path}, status_code=status)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app
