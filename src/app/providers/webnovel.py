"""Book metadata exposed by QQ Reading's public catalog."""

import requests

from app.providers import services

SOURCE = "webnovel"
BASE_URL = "https://book.qq.com"


def request(path, **params):
    """Read public catalog data with a bounded timeout."""
    try:
        response = services.session.get(
            f"{BASE_URL}/api/{path}",
            params=params,
            headers={"User-Agent": "Mozilla/5.0 ACGLib/1.0", "Referer": BASE_URL},
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError("QQ 阅读未返回有效资料")
        return payload["data"]
    except (requests.RequestException, ValueError, AttributeError) as error:
        raise services.ProviderAPIError(SOURCE, error) from error


def result(book):
    """Convert catalog fields into a selectable book."""
    bid = int(book["bid"])
    cover = book.get("cover") or (
        f"https://wfqqreader-1252317822.image.myqcloud.com/cover/{bid % 1000}/{bid}/b_{bid}.jpg"
    )
    if cover.startswith("//"):
        cover = f"https:{cover}"
    return {
        "source": SOURCE,
        "media_type": "book",
        "media_id": str(bid),
        "title": book["title"],
        "original_title": "",
        "author": book.get("author") or "",
        "image": cover,
        "year": "",
        "format": "网络小说",
        "source_url": f"{BASE_URL}/book-detail/{bid}",
    }


def search(query):
    """Search the catalog without fetching chapter content."""
    data = request("booksearch/query", keyWord=query, pageNo=1, pageSize=20)
    return [result(book) for book in data.get("bookList", [])]


def metadata(media_id):
    """Return the metadata consumed by the library detail view."""
    data = request("book/detail", bid=media_id)
    if not data.get("bid") or not data.get("title"):
        services.raise_not_found_error(SOURCE, media_id, "book")
    category = data.get("category3Name") or data.get("category2Name") or ""
    return {
        **result(data),
        "synopsis": data.get("intro") or "暂无简介。",
        "max_progress": None,
        "details": {"author": data.get("author", ""), "format": "网络小说"},
        "genres": [category] if category else [],
        "related": {},
    }
