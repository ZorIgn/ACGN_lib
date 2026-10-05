"""SQLite-backed metadata snapshots with bounded background refreshes."""

import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import requests
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, close_old_connections, connections
from django.utils import timezone

from app.models import LibraryCacheEntry

MAX_AGE = 3600
RETRY_AFTER = 300
PUBLIC_ENTRY_LIMIT = 512
MAX_PENDING = 8
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="library-refresh")
_pending = set()
_pending_lock = threading.Lock()
_store_lock = threading.RLock()
_load_locks = [threading.Lock() for _ in range(32)]
_context = threading.local()


class Unavailable(requests.RequestException):
    """A resource has no usable snapshot during its retry interval."""


def storage_key(key, owner=None):
    """Include the owner in every cache address, including private snapshots."""
    identity = f"{owner.pk if owner else 'public'}:{key}"
    return "library:" + hashlib.sha256(identity.encode()).hexdigest()


def read(key, *, owner=None):
    """Return an owner-bound snapshot, including its successful save time."""
    identifier = storage_key(key, owner)
    value = cache.get(identifier)
    if value is None:
        with _store_lock:
            value = (
                LibraryCacheEntry.objects.filter(key=identifier, owner=owner)
                .values("payload", "updated_at")
                .first()
            )
        if value is not None:
            cache.set(identifier, value, MAX_AGE)
    return copy.deepcopy(value)


def write(key, payload, *, owner=None):
    """Persist one bounded snapshot without retaining request/session objects."""
    # Normalize Decimal and tuples before comparing data across process restarts.
    payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
    identifier = storage_key(key, owner)
    with _store_lock:
        row, _ = LibraryCacheEntry.objects.update_or_create(
            key=identifier, defaults={"owner": owner, "payload": payload}
        )
        value = {"payload": payload, "updated_at": row.updated_at}
        cache.set(identifier, value, MAX_AGE)
        if owner is None:
            excess = list(
                LibraryCacheEntry.objects.filter(owner=None)
                .order_by("-updated_at", "-pk")
                .values_list("key", flat=True)[PUBLIC_ENTRY_LIMIT:]
            )
            if excess:
                LibraryCacheEntry.objects.filter(key__in=excess, owner=None).delete()
                cache.delete_many(excess)
    return copy.deepcopy(value)


def fresh(snapshot, max_age=MAX_AGE):
    """Check freshness against the persisted timestamp, not process uptime."""
    return bool(
        snapshot and (timezone.now() - snapshot["updated_at"]).total_seconds() < max_age
    )


def cooling_down(key, *, owner=None):
    """Avoid retrying an unavailable source on every incoming request."""
    return bool(cache.get(storage_key(key, owner) + ":retry"))


def refreshing(key, *, owner=None):
    """Report queued work even when the last successful snapshot is still fresh."""
    with _pending_lock:
        return storage_key(key, owner) in _pending


def schedule(key, load, *, owner=None, retry=False):
    """Queue at most eight refreshes; repeated requests reuse an in-flight job."""
    identifier = storage_key(key, owner)
    with _pending_lock:
        if identifier in _pending:
            return True
        if len(_pending) >= MAX_PENDING or (
            not retry and cooling_down(key, owner=owner)
        ):
            return False
        _pending.add(identifier)

    def run():
        from app.providers.services import ProviderAPIError  # noqa: PLC0415

        close_old_connections()
        try:
            load()
            cache.delete(identifier + ":retry")
        except (
            ProviderAPIError,
            requests.RequestException,
            DatabaseError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            cache.set(identifier + ":retry", 1, RETRY_AFTER)
        finally:
            connections.close_all()
            with _pending_lock:
                _pending.discard(identifier)

    try:
        _executor.submit(run)
    except RuntimeError:
        with _pending_lock:
            _pending.discard(identifier)
        return False
    return True


@contextmanager
def refresh_context(*, synchronous=False, retry=False):
    """Collect stale reads and refresh inline inside a background ranking job."""
    previous = getattr(_context, "value", None)
    state = {"synchronous": synchronous, "retry": retry, "stale": False}
    _context.value = state
    try:
        yield state
    finally:
        _context.value = previous


def get_or_load(key, load, *, owner=None, max_age=MAX_AGE):
    """Serve saved metadata immediately and refresh expired data when possible."""
    from app.providers.services import ProviderAPIError  # noqa: PLC0415

    snapshot = read(key, owner=owner)
    if fresh(snapshot, max_age):
        return snapshot["payload"]
    state = getattr(_context, "value", None)
    synchronous = state and state["synchronous"]
    if snapshot and not synchronous:
        if state is not None:
            state["stale"] = True
        schedule(key, lambda: _load(key, load, owner, max_age), owner=owner)
        return snapshot["payload"]
    try:
        return _load(key, load, owner, max_age, retry=bool(state and state["retry"]))
    except (
        ProviderAPIError,
        requests.RequestException,
        DatabaseError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        if snapshot is None:
            raise
        if state is not None:
            state["stale"] = True
        return snapshot["payload"]


def _load(key, load, owner, max_age, *, retry=False):
    from app.providers.services import ProviderAPIError  # noqa: PLC0415

    identifier = storage_key(key, owner)
    with _load_locks[hash(identifier) % len(_load_locks)]:
        current = read(key, owner=owner)
        if fresh(current, max_age):
            return current["payload"]
        if not retry and cooling_down(key, owner=owner):
            raise Unavailable("Metadata refresh is waiting for the retry interval.")
        try:
            payload = load()
            if payload is None:
                raise Unavailable("Metadata source returned no document.")
            stored = write(key, payload, owner=owner)
            cache.delete(identifier + ":retry")
            return stored["payload"]
        except (
            ProviderAPIError,
            requests.RequestException,
            DatabaseError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            cache.set(identifier + ":retry", 1, RETRY_AFTER)
            raise
