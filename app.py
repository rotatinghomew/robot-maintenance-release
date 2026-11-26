#!/usr/bin/env python3
"""Local entry point and conventional WSGI export.

Run ``python3 app.py --port 8000``.  ``app``/``application`` are WSGI callables
for in-process tests; :data:`service` is the framework-free domain singleton.
"""
from __future__ import annotations

from backend.service import MaintenanceService, ApiError, service
from backend.server import (
    ApiRouter,
    create_server,
    main,
    make_handler,
    normalize_path,
    wsgi_app as app,
)

application = app
router = ApiRouter(service)


def create_app(*_args, **_kwargs):
    """Return the WSGI callable; provided for conventional test imports."""
    return app


def run(port: int = 8000, host: str = "127.0.0.1") -> None:
    main(["--host", host, "--port", str(port)])


__all__ = [
    "ApiError",
    "ApiRouter",
    "MaintenanceService",
    "app",
    "application",
    "create_app",
    "create_server",
    "main",
    "make_handler",
    "normalize_path",
    "router",
    "run",
    "service",
]


if __name__ == "__main__":
    main()
