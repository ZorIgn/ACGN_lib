"""Shared server health checks for both public URL configurations."""

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from health_check.views import HealthCheckView
from redis.asyncio import Redis as RedisClient


def health_view():
    """Check the database, cache and persistent background-worker infrastructure."""
    return login_not_required(
        HealthCheckView.as_view(
            checks=[
                "health_check.Cache",
                "health_check.Database",
                "health_check.contrib.celery.Ping",
                (
                    "health_check.contrib.redis.Redis",
                    {
                        "client_factory": lambda: RedisClient.from_url(
                            settings.REDIS_URL
                        )
                    },
                ),
            ]
        )
    )
