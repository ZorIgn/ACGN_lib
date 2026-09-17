"""Run the private library and its background work in one Windows process."""

import argparse
import importlib
import os
import secrets
import sys
import threading
from pathlib import Path


def main():
    """Initialize persistent data and serve the local application."""
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    parser = argparse.ArgumentParser(description="私人作品书架")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    env_file = root / ".env"
    if not env_file.exists():
        env_file.write_text(
            f"SECRET={secrets.token_urlsafe(48)}\nTZ=Asia/Hong_Kong\nTMDB_LANG=zh-CN\n",
            encoding="utf-8",
        )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.native_settings")

    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from waitress import serve
    from whitenoise import WhiteNoise

    from app.models import SteamConnection
    from config.celery import app

    call_command("migrate", interactive=False, verbosity=0)
    call_command("collectstatic", interactive=False, verbosity=0)
    SteamConnection.objects.filter(running=True).update(
        running=False, result="上次同步被中断，请重新同步。"
    )
    app.autodiscover_tasks(force=True)
    importlib.import_module("app.library_tasks")
    worker = app.Worker(
        pool="solo",
        concurrency=1,
        loglevel="WARNING",
        without_heartbeat=True,
        without_gossip=True,
        without_mingle=True,
    )
    threading.Thread(target=worker.start, name="library-jobs", daemon=True).start()
    beat = app.Beat(loglevel="WARNING", max_interval=15)
    threading.Thread(target=beat.run, name="library-schedule", daemon=True).start()
    application = WhiteNoise(
        get_wsgi_application(), root=settings.STATIC_ROOT, prefix=settings.STATIC_URL
    )
    print(f"书架已启动：http://127.0.0.1:{args.port}/library/", flush=True)
    serve(
        application,
        host="0.0.0.0",
        port=args.port,
        threads=4,
        ident="ACGLib",
        channel_timeout=90,
    )


if __name__ == "__main__":
    main()
