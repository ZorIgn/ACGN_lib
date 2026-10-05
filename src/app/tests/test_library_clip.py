"""A browser capture stays editable and never imports before confirmation."""

from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from app.models import Book, LibraryImportDraft


class ClipTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="clip-owner")
        self.client.force_login(self.user)

    @patch("app.library.lookup")
    def test_bookmark_get_only_prefills_and_escapes_page_data(self, lookup):
        response = self.client.get(
            "/library/clip/",
            {
                "title": "<script>alert(1)</script>",
                "url": "https://example.com/read?chapter=32",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LibraryImportDraft.objects.exists())
        lookup.assert_not_called()
        self.assertNotContains(response, "<script>alert(1)</script>")
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(
            soup.select_one("[name=title]")["value"], "<script>alert(1)</script>"
        )
        script = soup.select_one("[data-bookmarklet]")["href"]
        self.assertTrue(script.startswith("javascript:"))
        self.assertIn("http://testserver/library/clip/", script)
        self.assertIn("document.title", script)
        self.assertIn("location.href", script)

    @patch("app.library.seed_draft")
    @patch("app.providers.services.search", return_value={"results": []})
    def test_search_produces_review_with_link_and_import_is_explicit(
        self, search, seed
    ):
        url = "https://example.com/read/32#paragraph-2"
        response = self.client.post(
            "/library/clip/", {"title": "星河", "url": url, "media_type": "manga"}
        )
        self.assertEqual(response.status_code, 302)
        draft = LibraryImportDraft.objects.get(user=self.user)
        self.assertFalse(Book.objects.exists())
        self.assertEqual(draft.entries[0]["viewing_url"], url)
        self.assertEqual(draft.entries[0]["manual_title"], "星河")
        self.assertEqual(draft.return_url, "/library/clip/")
        review = self.client.get(response.url)
        self.assertContains(review, url)
        self.assertContains(review, "自定义作品")

    @patch("app.library.lookup")
    def test_invalid_title_type_and_unsafe_link_never_make_draft(self, lookup):
        for changed in [
            {"url": "javascript:alert(1)"},
            {"title": ""},
            {"title": "字" * 501},
            {"media_type": "invalid"},
            {"url": "file:///C:/secret"},
        ]:
            response = self.client.post(
                "/library/clip/",
                {
                    "title": "星河",
                    "url": "https://example.com/",
                    "media_type": "book",
                    **changed,
                },
            )
            self.assertContains(response, "errorlist")
        self.assertFalse(LibraryImportDraft.objects.exists())
        lookup.assert_not_called()

    def test_authentication_csrf_and_method(self):
        self.client.logout()
        self.assertEqual(self.client.get("/library/clip/").status_code, 302)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post("/library/clip/", {}).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.put("/library/clip/").status_code, 405)
