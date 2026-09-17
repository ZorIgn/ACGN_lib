"""Background Steam synchronization using stable Steam application IDs."""

import requests
from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from app.models import Game, Item, Sources, Status, SteamConnection
from integrations.imports.helpers import decrypt


def fetch_games(connection):
    """Fetch the public game library; keep the API key out of task messages."""
    try:
        response = requests.get(
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/",
            params={
                "key": decrypt(connection.encrypted_key),
                "steamid": connection.steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
            timeout=(5, 30),
        )
        if response.status_code in {401, 403}:
            raise ValueError("Steam 拒绝访问，请检查 API 密钥与资料隐私设置。")
        response.raise_for_status()
        payload = response.json().get("response", {})
    except requests.RequestException:
        raise ValueError("暂时无法连接 Steam，请稍后重试。") from None
    if "games" not in payload:
        if payload.get("game_count") == 0:
            return []
        raise ValueError(
            "Steam 未提供游戏列表，请把个人资料和游戏详情设为公开，并允许显示游玩时长。"
        )
    if not isinstance(payload["games"], list):
        raise ValueError("Steam 返回的游戏列表格式异常，请稍后重试。")
    return payload["games"]


@shared_task(name="app.library_tasks.sync_steam")
def sync_steam(user_id):
    """Update time atomically while retaining personal ratings and decisions."""
    if not SteamConnection.objects.filter(user_id=user_id, running=False).update(
        running=True, result="正在同步…"
    ):
        return "同步已在进行，或连接不存在。"
    saved = SteamConnection.objects.get(user_id=user_id)
    try:
        games = fetch_games(saved)
        created = updated = 0
        with transaction.atomic():
            for data in games:
                app_id = str(int(data["appid"]))
                minutes = max(0, int(data.get("playtime_forever", 0)))
                item, _ = Item.objects.get_or_create(
                    source=Sources.STEAM,
                    media_type="game",
                    media_id=app_id,
                    defaults={
                        "title": data.get("name") or f"Steam {app_id}",
                        "image": f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/library_600x900.jpg",
                    },
                )
                existing = Game.objects.filter(user_id=user_id, item=item).first()
                if existing:
                    if existing.progress != minutes:
                        existing.progress = minutes
                        models.Model.save(existing, update_fields=["progress"])
                        updated += 1
                else:
                    status = (
                        Status.PLANNING
                        if not minutes
                        else (
                            Status.IN_PROGRESS
                            if data.get("playtime_2weeks")
                            else Status.PAUSED
                        )
                    )
                    record = Game(
                        user_id=user_id, item=item, progress=minutes, status=status
                    )
                    models.Model.save(record)
                    created += 1
        result = f"同步完成：新增 {created} 部，更新 {updated} 部游玩时长。"
        SteamConnection.objects.filter(pk=saved.pk).update(last_synced=timezone.now())
    except ValueError as error:
        result = str(error)
    except Exception:
        result = "同步未完成，已有记录保持原样。请稍后重试。"
    SteamConnection.objects.filter(pk=saved.pk).update(running=False, result=result)
    return result
