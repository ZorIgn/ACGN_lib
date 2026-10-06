"""Library routes plus the server health endpoint used by Docker probes."""

from django.urls import path

from config.health import health_view

from .native_urls import urlpatterns as library_patterns

urlpatterns = [*library_patterns, path("health/", health_view())]
