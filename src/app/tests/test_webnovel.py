"""Public catalog conversion and supplemental book lookup."""

from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from app import library
from app.providers import services, webnovel


class WebNovelTests(SimpleTestCase):
    def book(self):
        return {"bid": 1234, "title": "海上的书", "author": "作者", "cover": ""}

    @patch("app.providers.webnovel.services.session.get")
    def test_search_maps_public_fields_and_cover(self, get):
        get.return_value.json.return_value = {
            "code": 0,
            "data": {"bookList": [self.book()]},
        }
        result = webnovel.search("海上的书")[0]
        self.assertEqual(result["media_id"], "1234")
        self.assertEqual(result["author"], "作者")
        self.assertTrue(result["image"].endswith("/234/1234/b_1234.jpg"))
        self.assertEqual(result["source_url"], "https://book.qq.com/book-detail/1234")
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 15))

    @patch("app.providers.webnovel.request")
    def test_metadata_dispatch(self, request):
        request.return_value = {**self.book(), "intro": "简介", "category3Name": "奇幻"}
        result = services.get_media_metadata("book", "1234", "webnovel")
        self.assertEqual(result["synopsis"], "简介")
        self.assertEqual(result["genres"], ["奇幻"])
        self.assertIsNone(result["max_progress"])

    @patch("app.providers.webnovel.services.session.get")
    def test_provider_errors_are_reported(self, get):
        for response in (
            Mock(json=Mock(return_value={"code": -1, "data": None})),
            Mock(json=Mock(side_effect=ValueError("invalid json"))),
        ):
            get.return_value = response
            with self.assertRaises(services.ProviderAPIError):
                webnovel.search("书名")
        get.side_effect = requests.Timeout("unavailable")
        with self.assertRaises(services.ProviderAPIError):
            webnovel.search("书名")

    @patch("app.library.webnovel.search")
    @patch("app.library.services.search")
    def test_unrelated_titles_use_supplemental_catalog(self, search, novel_search):
        search.return_value = {"results": [{"title": "另一本书"}]}
        novel_search.return_value = [webnovel.result(self.book())]
        row = library.lookup({"input": "海上的书", "media_type": "book"})
        self.assertEqual(row["choices"][0]["source"], "webnovel")
        search.side_effect = services.ProviderAPIError("bangumi", ValueError("offline"))
        row = library.lookup({"input": "海上的书", "media_type": "book"})
        self.assertEqual(row["choices"][0]["source"], "webnovel")
