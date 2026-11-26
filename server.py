#!/usr/bin/env python3
"""Convenience wrapper: ``python3 server.py``."""
from app import app, application
from backend.server import ApiRouter, create_server, main, make_handler
from backend.service import MaintenanceService, service

__all__ = [
    "ApiRouter",
    "MaintenanceService",
    "app",
    "application",
    "create_server",
    "main",
    "make_handler",
    "service",
]


if __name__ == "__main__":
    main()
