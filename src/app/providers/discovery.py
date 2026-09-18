"""Public catalog documents used for discovery and local taste matching."""

import hashlib
import json
import re

import requests
from django.core.cache import cache

from app.providers import bangumi, tmdb, webnovel


def public_json(url, *, params=None, payload=None):
    """Fetch public metadata with a short timeout and no account credentials."""
    response = requests.request(
        "POST" if payload is not None else "GET",
        url,
        params=params,
        json=payload,
        headers={
            "User-Agent": "ACGLib/1.0 (personal media library)",
            "Referer": "https://book.qq.com/",
        },
        timeout=(3, 8),
    )
    response.raise_for_status()
    return response.json()


def cached(key, load):
    """Share public catalog data without retaining private preference profiles."""
    key = "discovery:" + hashlib.sha256(key.encode()).hexdigest()
    value = cache.get(key)
    if value is None:
        value = load()
        cache.set(key, value, 3600)
    return value


def bgm_document(data):
    """Keep public features and popularity alongside the importable candidate."""
    candidate = bangumi.result(data)
    rating = data.get("rating") or {}
    tags = [tag["name"] for tag in data.get("tags", []) if tag.get("name")]
    return {
        **candidate,
        "tags": tags[:15],
        "summary": data.get("summary", "")[:1500],
        "heat": sum((data.get("collection") or {}).values()),
        "community_score": rating.get("score") or 0,
        "votes": rating.get("total") or 0,
        "source_label": "Bangumi",
    }


def novel_document(data):
    """Read book metadata without retaining chapter content."""
    return {
        **webnovel.result(data),
        "tags": [
            data[key] for key in ("category2Name", "category3Name") if data.get(key)
        ],
        "summary": data.get("intro", "")[:1500],
        "heat": data.get("readingNum") or data.get("favorCount") or 0,
        "community_score": float(data.get("score") or 0),
        "votes": data.get("scoreNum") or 0,
        "source_label": "QQ 阅读",
    }


def movie_document(data, kind):
    """Use TMDB genre identifiers consistently for seeds and candidates."""
    return {
        "source": "tmdb",
        "media_type": kind,
        "media_id": str(data["id"]),
        "title": data.get("title") or data.get("name"),
        "original_title": data.get("original_title") or data.get("original_name") or "",
        "image": tmdb.get_image_url(data.get("poster_path")),
        "tags": [
            f"题材:{value}"
            for value in data.get(
                "genre_ids", [g["id"] for g in data.get("genres", [])]
            )
        ],
        "summary": data.get("overview", "")[:1500],
        "author": "",
        "heat": data.get("popularity") or 0,
        "community_score": data.get("vote_average") or 0,
        "votes": data.get("vote_count") or 0,
        "source_label": "TMDB",
    }


def bangumi_catalog(kind, tag=""):
    """Retrieve a popular or tag-constrained pool in the selected medium."""

    def load():
        tags = ["小说"] if kind == "book" else ["漫画"] if kind == "manga" else []
        if tag and tag not in tags:
            tags.append(tag)
        data = public_json(
            f"{bangumi.BASE_URL}/search/subjects",
            params={"limit": 48, "offset": 0},
            payload={
                "keyword": "",
                "sort": "heat",
                "filter": {
                    "type": [bangumi.SUBJECT_TYPES[kind]],
                    "tag": tags,
                    "nsfw": False,
                },
            },
        )
        return [
            bgm_document(row)
            for row in data.get("data", [])
            if bangumi.media_type(row) == kind
        ]

    return cached(f"bangumi:{kind}:{tag}", load)


def novel_catalog(author=""):
    """Read the public charts or search for an author's other works."""

    def load():
        if author:
            payload = public_json(
                f"{webnovel.BASE_URL}/api/booksearch/query",
                params={"keyWord": author, "pageNo": 1, "pageSize": 20},
            )
            rows = [
                row
                for row in payload.get("data", {}).get("bookList", [])
                if row.get("author") == author
            ]
        else:
            payload = public_json(f"{webnovel.BASE_URL}/api/recommend/query")
            rows = []
            for channel in payload.get("data", {}).values():
                for chart in channel.values():
                    for index, row in enumerate(chart.get("bookList", [])):
                        rows.append({**row, "chart_position": index + 1})
        return [
            {**novel_document(row), "chart_position": row.get("chart_position")}
            for row in rows
        ]

    return cached(f"webnovel:{author}", load)


def movie_catalog(kind, seed_id=""):
    """Read public popular titles or recommendations associated with one title."""

    def load():
        path = f"{kind}/{seed_id}/recommendations" if seed_id else f"{kind}/popular"
        data = public_json(
            f"{tmdb.base_url}/{path}", params={**tmdb.base_params, "page": 1}
        )
        return [
            movie_document(row, kind)
            for row in data.get("results", [])
            if not row.get("adult")
        ]

    return cached(f"tmdb:{kind}:{seed_id}", load)


def describe(source, kind, media_id, title):
    """Load one public seed document; private ratings never leave the machine."""

    def load():
        if source == "bangumi":
            return bgm_document(public_json(f"{bangumi.BASE_URL}/subjects/{media_id}"))
        if source == "webnovel":
            data = public_json(
                f"{webnovel.BASE_URL}/api/book/detail", params={"bid": media_id}
            )
            return novel_document(data["data"])
        if source == "tmdb":
            return movie_document(
                public_json(
                    f"{tmdb.base_url}/{kind}/{media_id}", params=tmdb.base_params
                ),
                kind,
            )
        if source == "steam":
            data = public_json(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": media_id, "l": "schinese", "cc": "CN"},
            )
            data = data[str(media_id)].get("data", {})
            return {
                "source": source,
                "media_type": kind,
                "media_id": str(media_id),
                "title": title,
                "original_title": data.get("name", ""),
                "tags": [row["description"] for row in data.get("genres", [])],
                "summary": re.sub(r"<[^>]+>", " ", data.get("short_description", "")),
                "author": "、".join(data.get("developers", [])),
            }
        return {"title": title, "tags": [], "summary": "", "author": ""}

    return cached(json.dumps([source, kind, media_id, title], ensure_ascii=False), load)
