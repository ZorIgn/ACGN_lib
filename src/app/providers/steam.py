"""Metadata for Steam games without an external catalog match."""

from app.models import Item, MediaTypes, Sources


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
        "source_url": f"https://store.steampowered.com/app/{media_id}/",
        "synopsis": "",
        "max_progress": None,
        "score": None,
        "score_count": None,
        "details": {},
        "related": {},
    }
