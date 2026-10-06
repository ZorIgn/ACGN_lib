"""ACGLib library UI with the persistent server broker and worker settings."""

from . import settings as server_settings
from .native_settings import *  # noqa: F403
from .settings import (  # noqa: F401
    CACHES,
    CELERY_BEAT_SCHEDULE,
    CELERY_BROKER_URL,
    CELERY_WORKER_MAX_TASKS_PER_CHILD,
)

CELERY_WORKER_POOL = "prefork"

CELERY_BROKER_TRANSPORT_OPTIONS = getattr(
    server_settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {}
)

ROOT_URLCONF = "config.container_urls"

CELERY_IMPORTS = ("app.library_tasks",)
