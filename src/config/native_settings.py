"""Settings for the single-process Windows installation."""

from .settings import *  # noqa: F403

NATIVE_RUNTIME = True
ROOT_URLCONF = "config.native_urls"
LANGUAGE_CODE = "zh-hans"
LOGIN_REDIRECT_URL = "library"
ACCOUNT_ADAPTER = "users.account_adapter.FirstUserAccountAdapter"
ALLAUTH_TRUSTED_CLIENT_IP_HEADER = None
SECURE_PROXY_SSL_HEADER = None

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "acglib",
        "TIMEOUT": 86400,
        "OPTIONS": {"MAX_ENTRIES": 2000},
    },
}
CELERY_BROKER_URL = "memory://"
CELERY_BROKER_TRANSPORT_OPTIONS = {}
CELERY_WORKER_POOL = "solo"
CELERY_WORKER_REDIRECT_STDOUTS = False
CELERY_WORKER_MAX_TASKS_PER_CHILD = None
CELERY_BEAT_MAX_LOOP_INTERVAL = 15
CELERY_BEAT_SCHEDULE = {}
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "debug_toolbar"]  # noqa: F405
STATIC_URL = "/static/"
TEMPLATES[0]["DIRS"].insert(0, BASE_DIR / "templates" / "native")  # noqa: F405

MIDDLEWARE = [  # noqa: F405
    middleware
    for middleware in MIDDLEWARE  # noqa: F405
    if middleware != "debug_toolbar.middleware.DebugToolbarMiddleware"
]
