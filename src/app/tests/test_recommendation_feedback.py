"""Private recommendation dismissals, persistent exclusions and undo."""

from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.db import models
from django.test import TestCase

from app import library_discovery, recommendations
from app.models import Book, Item, LibraryRecommendationDismissal
from app.providers import discovery
from app.tests.test_recommendations import document


class RecommendationFeedbackTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user(username="feedback-owner")
        self.other = get_user_model().objects.create_user(username="feedback-other")
        self.client.force_login(self.owner)
        self.catalog = [document(i, f"推荐作品 {i}", ["冒险"]) for i in range(80)]
        for name, value in [("bangumi_catalog", self.catalog), ("novel_catalog", [])]:
            mock = patch.object(discovery, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        describe = patch.object(discovery, "describe", return_value=None)
        describe.start()
        self.addCleanup(describe.stop)

    def token(self, index=7, user=None):
        return signing.dumps(
            {"user": (user or self.owner).pk, "candidate": self.catalog[index]},
            salt=library_discovery.TOKEN_SALT,
        )

    def feedback(self, action="dismiss", token=None):
        return self.client.post(
            "/library/recommendations/feedback/",
            {"action": action, "candidate": token or self.token()},
        )

    def ids(self, mode="personal", user=None):
        return [
            item["media_id"]
            for item in recommendations.batch(user or self.owner, "book", mode)["items"]
        ]

    def test_dismissal_is_private_idempotent_and_undo_is_user_bound(self):
        token = self.token()
        for _ in range(2):
            response = self.feedback(token=token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["candidate"], token)
        dismissal = LibraryRecommendationDismissal.objects.get()
        self.assertEqual(dismissal.user, self.owner)
        self.assertEqual(dismissal.title, self.catalog[7]["title"])
        self.assertEqual(dismissal.media_type, "book")
        self.client.force_login(self.other)
        self.assertEqual(self.feedback(token=token).status_code, 400)
        self.assertEqual(self.feedback("undo", token=token).status_code, 400)
        self.assertEqual(
            self.feedback(token=self.token(user=self.other)).status_code, 200
        )
        self.client.force_login(self.owner)
        self.assertEqual(self.feedback("undo", token=token).status_code, 200)
        self.assertEqual(
            list(
                LibraryRecommendationDismissal.objects.values_list("user_id", flat=True)
            ),
            [self.other.pk],
        )

    def test_invalid_expired_or_missing_tokens_cannot_change_feedback(self):
        tokens = ["tampered", signing.dumps({}, salt=library_discovery.TOKEN_SALT)]
        with patch("django.core.signing.time.time", return_value=1):
            tokens.append(self.token())
        for token in tokens:
            self.assertEqual(self.feedback(token=token).status_code, 400)
        self.assertEqual(self.feedback(action="invalid").status_code, 400)
        self.assertFalse(LibraryRecommendationDismissal.objects.exists())
        self.assertEqual(
            self.client.get("/library/recommendations/feedback/").status_code, 405
        )
        self.client.logout()
        self.assertEqual(self.feedback().status_code, 302)

    def test_both_modes_exclude_feedback_across_cache_and_session_refresh(self):
        for mode in ["personal", "popular"]:
            self.assertIn("7", self.ids(mode))
        self.assertEqual(self.feedback().status_code, 200)
        for mode in ["personal", "popular"]:
            self.assertNotIn("7", self.ids(mode))
            self.assertIn("7", self.ids(mode, self.other))
        cache.clear()
        self.client.logout()
        self.client.force_login(self.owner)
        for mode in ["personal", "popular"]:
            response = self.client.get("/library/recommendations/", {"mode": mode})
            self.assertNotIn(
                "7", [item["media_id"] for item in response.context["items"]]
            )

    def test_dismissed_slot_is_filled_without_moving_other_cards(self):
        before = {mode: self.ids(mode) for mode in ["personal", "popular"]}
        response = self.feedback()
        self.assertTrue(response.json()["ok"])
        for mode, original in before.items():
            hidden_slot = original.index("7")
            after = self.ids(mode)
            self.assertEqual(len(after), 50)
            self.assertNotIn(after[hidden_slot], original)
            for index, media_id in enumerate(original):
                if index != hidden_slot:
                    self.assertEqual(after[index], media_id)
            self.assertEqual(self.ids(mode), after)
        self.assertEqual(
            self.feedback("undo", response.json()["candidate"]).status_code, 200
        )
        for mode in before:
            self.assertIn("7", self.ids(mode))

    def test_undo_latest_keeps_earlier_dismissals_and_owned_works_excluded(self):
        item = Item.objects.create(
            source="bangumi",
            media_type="book",
            media_id="3",
            title=self.catalog[3]["title"],
        )
        models.Model.save(Book(user=self.owner, item=item))
        self.feedback(token=self.token(7))
        latest = self.feedback(token=self.token(8)).json()["candidate"]
        self.assertNotIn("8", self.ids())
        self.assertEqual(self.feedback("undo", latest).status_code, 200)
        for mode in ["personal", "popular"]:
            result = self.ids(mode)
            self.assertNotIn("3", result)
            self.assertNotIn("7", result)
            self.assertIn("8", result)

    def test_card_feedback_uses_signed_candidate_without_submitting_add_form(self):
        response = self.client.get("/library/recommendations/")
        soup = BeautifulSoup(response.content, "html.parser")
        card = soup.select_one(".recommendation-card")
        button = card.select_one("[data-recommend-dismiss]")
        self.assertEqual(button["type"], "button")
        self.assertIsNone(button.find_parent("form"))
        self.assertEqual(
            button["data-candidate"], card.select_one('[name="candidate"]')["value"]
        )
        self.assertEqual(self.feedback(token=button["data-candidate"]).status_code, 200)
