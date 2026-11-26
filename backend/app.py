"""Alias for deployments/tests that import ``backend.app``."""
from .server import ApiRouter, create_server, main, make_handler, wsgi_app
from .service import ApiError, MaintenanceService, service

app = wsgi_app
application = wsgi_app
handler_class = make_handler(service)

__all__ = [
    "ApiRouter",
    "ApiError",
    "MaintenanceService",
    "app",
    "application",
    "create_server",
    "handler_class",
    "main",
    "make_handler",
    "service",
]
