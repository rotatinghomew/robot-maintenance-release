#!/usr/bin/env python3
"""Convenience wrapper: ``python3 main.py``."""
from app import app, application
from backend.server import create_server, main
from backend.service import MaintenanceService, service

__all__ = ["app", "application", "create_server", "main", "MaintenanceService", "service"]


if __name__ == "__main__":
    main()
