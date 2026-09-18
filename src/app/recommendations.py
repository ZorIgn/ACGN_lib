"""Lightweight content recommendations with explicit feedback and public heat."""

import hashlib
import json
import math
import re
import threading
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from django.apps import apps
from django.core.cache import cache

from app.providers import discovery

KINDS = {"book", "manga", "anime", "tv", "movie", "game"}
_batch_locks = [threading.Lock() for _ in range(16)]
GENERIC_TAGS = {
    "小说",
    "漫画",
    "动画",
    "游戏",
    "日本",
    "中国",
    "系列",
    "tv",
    "剧场版",
    "轻小说",
    "galgame",
    "日本动画",
}


def normalize(value):
    """Match catalog titles across punctuation, width and letter case."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(value)).casefold())


def features(document):
    """Combine whole tags and creators with modest Chinese bigram/text weights."""
    values = Counter()
    for tag in document.get("tags", []):
        term = normalize(tag)
        if (
            term
            and term not in GENERIC_TAGS
            and not re.fullmatch(r"\d+年?\d*月?", term)
        ):
            values[f"tag:{term}"] += 3
    author = normalize(document.get("author", ""))
    if author:
        values[f"author:{author}"] += 4
    text = f"{document.get('title', '')} {document.get('summary', '')}".casefold()
    for part in re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]+", text):
        tokens = (
            [part]
            if part.isascii()
            else [part[i : i + 2] for i in range(len(part) - 1)]
        )
        for token in tokens:
            values[f"text:{token}"] = min(0.6, values[f"text:{token}"] + 0.15)
    return values


def unit(vector):
    """Normalize a sparse vector for cosine similarity."""
    length = math.sqrt(sum(value * value for value in vector.values()))
    return {key: value / length for key, value in vector.items()} if length else {}


def similarity(left, right):
    """Compute cosine similarity for two normalized sparse vectors."""
    return sum(value * right.get(key, 0) for key, value in left.items())


def feedback(record):
    """Let explicit ratings outweigh completion and capped play-time signals."""
    if record.score is not None:
        return (float(record.score) - 5) / 5
    if record.status == "Dropped":
        return -0.4
    weight = {"Completed": 0.35, "In progress": 0.2, "Paused": 0.1}.get(
        record.status, 0.05
    )
    if record.item.media_type == "game" and record.progress:
        weight += min(0.2, math.log1p(record.progress) / 50)
    return weight


def safely(load):
    """Isolate a failed public source so other catalog results remain usable."""
    try:
        return load()
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        return None


def rank(candidates, seeds, owned, *, personal=True, limit=50):
    """Blend TF-IDF taste similarity with heat, then diversify the shortlist."""
    owned_ids = {(row.item.source, str(row.item.media_id)) for row in owned}
    owned_names = {normalize(row.item.title) for row in owned}
    for seed in seeds:
        owned_names.update(
            normalize(seed.get(field, "")) for field in ("title", "original_title")
        )
    owned_names.discard("")
    pool = []
    ids = set()
    names = set()
    for item in candidates:
        identity = (item["source"], str(item["media_id"]))
        aliases = {
            normalize(item.get(field, "")) for field in ("title", "original_title")
        } - {""}
        if identity in owned_ids or identity in ids or aliases & (owned_names | names):
            continue
        ids.add(identity)
        names.update(aliases)
        pool.append(dict(item))
    bags = [features(item) for item in [*pool, *seeds]]
    frequency = Counter(term for bag in bags for term in bag)
    vectors = [
        unit(
            {
                term: value * (1 + math.log((1 + len(bags)) / (1 + frequency[term])))
                for term, value in bag.items()
            }
        )
        for bag in bags
    ]
    positive, negative = Counter(), Counter()
    for seed, vector in zip(seeds, vectors[len(pool) :], strict=True):
        target = positive if seed["weight"] > 0 else negative
        for term, value in vector.items():
            target[term] += abs(seed["weight"]) * value
    positive, negative = unit(positive), unit(negative)
    maxima = {}
    for item in pool:
        maxima[item["source"]] = max(
            maxima.get(item["source"], 0), math.log1p(item.get("heat", 0))
        )
    meaningful = False
    for index, item in enumerate(pool):
        vector = vectors[index]
        affinities = [
            (similarity(vector, vectors[len(pool) + i]) * seed["weight"], seed)
            for i, seed in enumerate(seeds)
            if seed["weight"] > 0
        ]
        affinity, nearest = max(affinities, key=lambda pair: pair[0], default=(0, None))
        taste = max(similarity(vector, positive), affinity)
        heat = math.log1p(item.get("heat", 0)) / (maxima.get(item["source"]) or 1)
        if item.get("chart_position"):
            heat = max(heat, 1 / math.sqrt(item["chart_position"]))
        votes = item.get("votes", 0)
        quality = ((item.get("community_score", 0) / 10) * votes + 0.65 * 30) / (
            votes + 30
        )
        item["_score"] = (
            (0.72 * taste - 0.45 * similarity(vector, negative) if personal else 0)
            + 0.22 * heat
            + 0.06 * quality
        )
        item["_vector"] = vector
        if personal and nearest and affinity >= 0.08:
            meaningful = True
            score = nearest.get("personal_score")
            item["reason"] = (
                f"接近你给 {score:g} 分的《{nearest['title']}》"
                if score is not None
                else f"根据你收藏的《{nearest['title']}》"
            )
        else:
            item["reason"] = f"{item['source_label']} 热门作品"
    result = []
    while pool and len(result) < limit:
        best = max(
            pool,
            key=lambda item: (
                item["_score"]
                - (
                    0.12
                    * max(
                        (
                            similarity(item["_vector"], chosen["_vector"])
                            for chosen in result
                        ),
                        default=0,
                    )
                    if personal
                    else 0
                )
            ),
        )
        pool.remove(best)
        result.append(best)
    for item in result:
        item.pop("_vector")
        item.pop("_score")
    return result, meaningful


def recommend(user, kind, mode="personal", *, limit=50):
    """Build recommendations from the current owner's records and public pools."""
    records = list(
        apps.get_model("app", kind).objects.filter(user=user).select_related("item")
    )
    weighted = [(feedback(record), record) for record in records]
    selected = sorted(
        (pair for pair in weighted if pair[0] > 0),
        key=lambda pair: (pair[0], pair[1].pk),
        reverse=True,
    )[:5]
    selected += sorted(
        (pair for pair in weighted if pair[0] < 0), key=lambda pair: pair[0]
    )[:2]
    if mode == "popular":
        selected = []
    seed_loaders = [
        lambda record=record: discovery.describe(
            record.item.source, kind, record.item.media_id, record.item.title
        )
        for _, record in selected
    ]
    catalog_loaders = (
        [lambda: discovery.movie_catalog(kind)]
        if kind == "movie"
        else [lambda: discovery.bangumi_catalog(kind)]
    )
    if kind == "book":
        catalog_loaders.append(discovery.novel_catalog)
    with ThreadPoolExecutor(max_workers=4) as executor:
        loaded = list(executor.map(safely, [*seed_loaders, *catalog_loaders]))
    seeds = [
        {
            **document,
            "weight": weight,
            "personal_score": float(record.score) if record.score is not None else None,
        }
        for (weight, record), document in zip(
            selected, loaded[: len(selected)], strict=True
        )
        if document
    ]
    catalogs = [rows for rows in loaded[len(selected) :] if rows is not None]
    candidates = [item for rows in catalogs for item in rows]
    extra = []
    positive = [seed for seed in seeds if seed["weight"] > 0]
    tags = Counter()
    for seed in positive:
        for tag in seed.get("tags", []):
            if normalize(tag) not in GENERIC_TAGS and not re.search(r"\d", tag):
                tags[tag] += seed["weight"]
    if kind == "movie":
        extra = [
            lambda seed=seed: discovery.movie_catalog(kind, seed["media_id"])
            for seed in positive[:2]
            if seed.get("source") == "tmdb"
        ]
    else:
        extra = [
            lambda tag=tag: discovery.bangumi_catalog(kind, tag)
            for tag, _ in tags.most_common(2)
        ]
    if kind == "book":
        authors = list(
            dict.fromkeys(seed["author"] for seed in positive if seed.get("author"))
        )[:2]
        extra += [
            lambda author=author: discovery.novel_catalog(author) for author in authors
        ]
    with ThreadPoolExecutor(max_workers=3) as executor:
        for rows in executor.map(safely, extra):
            if rows:
                candidates.extend(rows)
    items, personalized = rank(
        candidates, seeds, records, personal=mode == "personal", limit=limit
    )
    return {
        "items": items,
        "personalized": personalized,
        "unavailable": not catalogs,
        "partial": any(value is None for value in loaded),
    }


def batch(user, kind, mode="personal"):
    """Reuse ranked pages until the owner's feedback or the public catalog expires."""
    key = f"recommendations:{user.pk}:{kind}:{mode}"
    with _batch_locks[hash(key) % len(_batch_locks)]:
        rows = list(
            apps.get_model("app", kind)
            .objects.filter(user=user)
            .order_by("pk")
            .values_list(
                "pk",
                "score",
                "status",
                "progress",
                "item__source",
                "item__media_id",
                "item__title",
            )
        )
        revision = hashlib.sha256(
            json.dumps(rows, default=str, ensure_ascii=False).encode()
        ).hexdigest()
        previous = cache.get(key)
        if previous and previous["revision"] == revision:
            return previous["result"]
        result = recommend(user, kind, mode, limit=80)
        if result.get("unavailable"):
            return result
        items = result["items"]
        if previous and all(row in rows for row in previous["records"]):
            owned = {(row[4], str(row[5])) for row in rows}
            names = {normalize(row[6]) for row in rows}

            def identity(item):
                return item["source"], str(item["media_id"])

            survivors = [
                item
                for item in previous["result"]["items"]
                if identity(item) not in owned and normalize(item["title"]) not in names
            ]
            kept = {identity(item) for item in survivors}
            titles = {normalize(item["title"]) for item in survivors}
            replacements = iter(
                item
                for item in items
                if identity(item) not in kept and normalize(item["title"]) not in titles
            )
            items = [
                item if identity(item) in kept else next(replacements, None)
                for item in previous["result"]["items"]
            ]
            items = [item for item in items if item is not None]
            items.extend(list(replacements)[: 50 - len(items)])
        result = {**result, "items": items[:50]}
        cache.set(key, {"revision": revision, "records": rows, "result": result}, 3600)
        return result
