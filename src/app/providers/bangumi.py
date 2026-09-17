"""Chinese metadata from the public Bangumi API."""

import math

import requests
from django.conf import settings
from django.core.cache import cache

from app.models import MediaTypes, Sources
from app.providers import services

BASE_URL = "https://api.bgm.tv/v0"
SUBJECT_TYPES = {"book": 1, "manga": 1, "anime": 2, "game": 4, "tv": 6}
HEADERS = {"User-Agent": "ACGLib/1.0 (personal media library)"}


def request(path, payload=None, params=None):
    """Read a public subject or search response with a bounded timeout."""
    try:
        response = services.session.request(
            "POST" if payload is not None else "GET",
            f"{BASE_URL}/{path}",
            json=payload,
            params=params,
            headers=HEADERS,
            timeout=(5, 20),
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise services.ProviderAPIError(Sources.BANGUMI, error) from error


def media_type(subject):
    """Distinguish adaptations sharing Bangumi's book subject type."""
    if subject.get("type") == 1:
        platform = subject.get("platform", "")
        return MediaTypes.MANGA.value if "漫画" in platform else MediaTypes.BOOK.value
    return {2: "anime", 4: "game", 6: "tv"}.get(subject.get("type"))


def image_url(subject):
    """Select the available cover."""
    images = subject.get("images") or {}
    return images.get("large") or images.get("common") or settings.IMG_NONE


def info_value(subject, key):
    """Flatten the public infobox value."""
    value = next(
        (entry["value"] for entry in subject.get("infobox", []) if entry["key"] == key),
        "",
    )
    if isinstance(value, list):
        return "、".join(str(entry.get("v", "")) for entry in value)
    return str(value)


def result(subject):
    """Convert a subject into a selectable library candidate."""
    return {
        "media_id": str(subject["id"]),
        "source": Sources.BANGUMI.value,
        "media_type": media_type(subject),
        "title": subject.get("name_cn") or subject["name"],
        "original_title": subject["name"],
        "image": image_url(subject),
        "format": subject.get("platform", ""),
        "year": (subject.get("date") or "")[:4],
        "author": info_value(subject, "作者"),
    }


def subject(media_id):
    """Read subject metadata, retaining it in the existing application cache."""
    key = f"bangumi_subject_{media_id}"
    data = cache.get(key)
    if data is None:
        data = request(f"subjects/{media_id}")
        cache.set(key, data)
    return data


def search(kind, query, page):
    """Search titles while retaining the requested work format."""
    if kind not in SUBJECT_TYPES:
        return {"page": page, "total_pages": 0, "total_results": 0, "results": []}
    key = f"bangumi_search_{kind}_{query}_{page}"
    data = cache.get(key)
    if data is not None:
        return data
    response = request(
        "search/subjects",
        {"keyword": query, "sort": "match", "filter": {"type": [SUBJECT_TYPES[kind]]}},
        {"limit": settings.PER_PAGE, "offset": (page - 1) * settings.PER_PAGE},
    )
    results = []
    for entry in response.get("data", []):
        if entry.get("type") == 1 and not entry.get("platform"):
            entry = subject(entry["id"])
        candidate = result(entry)
        if candidate["media_type"] == kind:
            results.append(candidate)
    total = response.get("total", 0)
    data = {
        "page": page,
        "total_results": total,
        "total_pages": math.ceil(total / settings.PER_PAGE),
        "results": results,
    }
    cache.set(key, data)
    return data


def metadata(media_id, kind):
    """Provide the metadata contract used by the existing tracking models."""
    data = subject(media_id)
    actual_type = media_type(data)
    if actual_type != kind:
        raise services.ProviderAPIError(Sources.BANGUMI, ValueError("作品类型不匹配"))
    max_progress = (
        (data.get("total_episodes") or data.get("eps") or None)
        if kind in {"anime", "manga"}
        else None
    )
    rating = data.get("rating") or {}
    return {
        **result(data),
        "source_url": f"https://bgm.tv/subject/{media_id}",
        "synopsis": data.get("summary") or "暂无简介。",
        "max_progress": max_progress,
        "score": rating.get("score"),
        "score_count": rating.get("total"),
        "genres": [],
        "details": {
            "format": data.get("platform", ""),
            "author": info_value(data, "作者"),
            "release_date": data.get("date"),
        },
        "related": {},
    }
