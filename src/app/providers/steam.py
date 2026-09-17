"""Metadata for Steam games without an external catalog match."""

import json

import requests

from app.models import Item, MediaTypes, Sources


def store_url(media_id):
    """Return the official store page for an application."""
    return f"https://store.steampowered.com/app/{media_id}/"


def covers(app_ids):
    """Read current artwork paths in bounded batches from the Steam store."""
    images = {}
    app_ids = list(dict.fromkeys(int(app_id) for app_id in app_ids))
    for offset in range(0, len(app_ids), 50):
        payload = {
            "ids": [{"appid": app_id} for app_id in app_ids[offset : offset + 50]],
            "context": {"language": "schinese", "country_code": "CN"},
            "data_request": {"include_assets": True},
        }
        try:
            response = requests.get(
                "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                params={"input_json": json.dumps(payload)},
                timeout=(5, 15),
            )
            response.raise_for_status()
            entries = response.json().get("response", {}).get("store_items", [])
            for entry in entries:
                assets = entry.get("assets", {})
                filename = assets.get("library_capsule") or assets.get("header")
                path = assets.get("asset_url_format", "")
                if (
                    filename
                    and path.startswith("steam/apps/")
                    and "${FILENAME}" in path
                ):
                    images[str(entry["appid"])] = (
                        "https://shared.akamai.steamstatic.com/store_item_assets/"
                        + path.replace("${FILENAME}", filename)
                    )
        except (
            requests.RequestException,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
        ):
            continue
    return images


def metadata(media_id):
    """Use the game information retained during account synchronization."""
    item = Item.objects.get(
        source=Sources.STEAM, media_type=MediaTypes.GAME, media_id=media_id
    )
    return {
        "media_id": item.media_id,
        "source": Sources.STEAM.value,
        "media_type": MediaTypes.GAME.value,
        "title": item.title,
        "image": item.image,
        "source_url": store_url(media_id),
        "synopsis": "",
        "max_progress": None,
        "score": None,
        "score_count": None,
        "details": {},
        "related": {},
    }
