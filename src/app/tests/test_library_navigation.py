"""Recommendation paging and return paths through reviewed imports."""

from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, LibraryImportDraft
from app.tests.test_recommendations import document


class NavigationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="navigation-owner")
        self.client.force_login(self.user)
        seed = patch("app.library.seed_draft")
        seed.start()
        self.addCleanup(seed.stop)
        self.items = [
            {**document(index, f"分页作品{index}", ["科幻"]), "reason": "资料源热门"}
            for index in range(1, 51)
        ]
        self.patch = patch(
            "app.recommendations.recommend",
            return_value={"items": self.items, "personalized": True},
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def choose(self, destination):
        response = self.client.get(
            "/library/recommendations/", {"type": "book", "page": 3}
        )
        token = BeautifulSoup(response.content, "html.parser").select_one(
            "[name=candidate]"
        )["value"]
        response = self.client.post(
            "/library/recommendations/choose/",
            {"candidate": token, "next": destination},
        )
        self.assertEqual(response.status_code, 302)
        return LibraryImportDraft.objects.get(user=self.user)

    def test_all_fifty_recommendations_can_be_paged_without_duplicates(self):
        seen = []
        for number in range(1, 6):
            response = self.client.get(
                "/library/recommendations/", {"type": "book", "page": number}
            )
            self.assertEqual(response.context["recommend_page"].number, number)
            seen.extend(item["media_id"] for item in response.context["items"])
            self.assertEqual(len(response.context["items"]), 2 if number == 5 else 12)
        self.assertEqual(seen, [str(index) for index in range(1, 51)])
        response = self.client.get("/library/recommendations/", {"page": 999})
        self.assertEqual(response.context["recommend_page"].number, 5)

    def test_save_and_import_return_to_same_recommendation_page(self):
        destination = "/library/add/?type=book&mode=popular&recommend_page=3"
        draft = self.choose(destination)
        payload = {
            "draft_id": str(draft.pk),
            "page": 1,
            "choice_0": 0,
            "0-0-score": 9,
            "action": "save_return",
        }
        response = self.client.post("/library/add/", payload)
        self.assertEqual(response.url, destination)
        self.assertFalse(Book.objects.filter(user=self.user).exists())
        draft.refresh_from_db()
        self.assertEqual(draft.entries[0]["personal"]["0"]["score"], "9")
        response = self.client.post("/library/add/", {**payload, "action": "import"})
        self.assertEqual(response.url, destination)
        self.assertEqual(Book.objects.get(user=self.user).status, "Completed")

    def test_return_targets_cannot_leave_library(self):
        for value in [
            "https://example.org/",
            "//example.org/",
            "/\\example.org",
            "/accounts/logout/",
            "javascript:alert(1)",
            "/library/\n",
        ]:
            draft = self.choose(value)
            response = self.client.post(
                "/library/add/",
                {"draft_id": str(draft.pk), "action": "save_return", "page": 1},
            )
            self.assertEqual(response.url, "/library/add/")
            draft.delete()

    def test_resuming_draft_updates_return_to_current_list(self):
        draft = self.choose("/library/add/?recommend_page=3")
        destination = "/library/add/?type=game&recommend_page=2"
        response = self.client.get(
            "/library/add/", {"draft": str(draft.pk), "next": destination}
        )
        self.assertEqual(response.context["return_url"], destination)
        draft.refresh_from_db()
        self.assertEqual(draft.return_url, destination)
        response = self.client.post(
            "/library/add/", {"draft_id": str(draft.pk), "save_return": "1", "page": 1}
        )
        self.assertEqual(response.url, destination)
