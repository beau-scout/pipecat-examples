"""Entrypoint: launch the EyePop Scout Monitor dashboard.

Usage:
    python run.py                 # uses config.yaml + .env
    python run.py --config my.yaml
    python run.py --source rtsp://user:pass@192.168.1.50/axis-media/media.amp
    python run.py --mock          # synthetic detections, no camera/API needed
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from app.config import load_settings
from app.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="EyePop Scout Monitor")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--source", default=None, help="override camera source (RTSP/file/index)")
    parser.add_argument("--mock", action="store_true", help="run with synthetic detections")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings(args.config)
    if args.source is not None:
        settings.camera.source = args.source
    if args.mock:
        settings.inference.mock = True
    if args.host is not None:
        settings.server.host = args.host
    if args.port is not None:
        settings.server.port = args.port

    app = create_app(settings)
    uvicorn.run(app, host=settings.server.host, port=settings.server.port,
                log_level=args.log_level)


if __name__ == "__main__":
    main()
