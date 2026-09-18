"""Recommendation reuse, stable replacement and quick rating imports."""

from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.db import models
from django.test import TestCase

from app import library_discovery, recommendations
from app.models import Book, Item, LibraryImportDraft
from app.tests.test_recommendations import document


class BatchTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="batch-owner")
        self.other = get_user_model().objects.create_user(username="batch-other")
        self.client.force_login(self.user)
        self.catalog = [
            {**document(i, f"候选 {i}", ["科幻"]), "reason": "相似题材"}
            for i in range(80)
        ]
        self.ranker = patch.object(recommendations, "recommend", side_effect=self.rank)
        self.mock = self.ranker.start()
        self.addCleanup(self.ranker.stop)

    def rank(self, user, kind, mode, **kwargs):
        owned = set(
            Book.objects.filter(user=user).values_list("item__media_id", flat=True)
        )
        return {
            "items": [
                dict(item) for item in self.catalog if item["media_id"] not in owned
            ],
            "personalized": True,
        }

    def token(self, index=7):
        return signing.dumps(
            {"user": self.user.pk, "candidate": self.catalog[index]},
            salt=library_discovery.TOKEN_SALT,
        )

    def add(self, **data):
        return self.client.post(
            "/library/recommendations/choose/",
            {"candidate": self.token(), "action": "quick_add", **data},
        )

    def test_pages_and_repeat_visits_reuse_one_ranked_batch(self):
        for page in [1, 2, 5, 3]:
            response = self.client.get("/library/recommendations/", {"page": page})
            soup = BeautifulSoup(response.content, "html.parser")
            self.assertEqual(len(soup.select(".recommendation-card")), 50)
            self.assertEqual(
                len(soup.select(".recommendation-card:not([hidden])")),
                2 if page == 5 else 12,
            )
        self.assertEqual(self.mock.call_count, 1)
        self.client.force_login(self.other)
        self.client.get("/library/recommendations/")
        self.assertEqual(self.mock.call_count, 2)

    def test_addition_fills_its_slot_without_shuffling_other_cards(self):
        before = recommendations.batch(self.user, "book")["items"]
        self.assertTrue(self.add(**{"quick-score": 9}).json()["created"])
        after = recommendations.batch(self.user, "book")["items"]
        self.assertEqual(len(after), 50)
        self.assertEqual(after[7]["media_id"], "50")
        for i in range(50):
            if i != 7:
                self.assertEqual(before[i]["media_id"], after[i]["media_id"])
        self.assertEqual(self.mock.call_count, 2)

    def test_rating_changes_invalidate_batch_and_other_users_do_not(self):
        self.add(**{"quick-score": 8})
        recommendations.batch(self.user, "book")
        record = Book.objects.get(user=self.user)
        Book.objects.filter(pk=record.pk).update(score=2)
        recommendations.batch(self.user, "book")
        self.assertEqual(self.mock.call_count, 2)
        models.Model.save(Book(user=self.other, item=record.item, score=10))
        recommendations.batch(self.user, "book")
        self.assertEqual(self.mock.call_count, 2)

    def test_quick_add_preserves_rating_defaults_and_is_idempotent(self):
        self.assertEqual(self.add(**{"quick-score": 0}).status_code, 200)
        record = Book.objects.get(user=self.user)
        self.assertEqual(record.score, 0)
        self.assertEqual(record.status, "Completed")
        self.assertFalse(self.add(**{"quick-score": 10}).json()["created"])
        record.refresh_from_db()
        self.assertEqual(record.score, 0)
        self.assertFalse(LibraryImportDraft.objects.exists())

    def test_quick_add_allows_empty_values_and_rejects_bad_ratings_or_signatures(self):
        self.assertEqual(self.add(**{"quick-score": 11}).status_code, 400)
        self.assertFalse(Item.objects.exists())
        self.add(**{"quick-score": 8, "quick-status_manual": "1", "quick-status": ""})
        self.assertEqual(Book.objects.get(user=self.user).status, "")
        self.client.force_login(self.other)
        self.assertEqual(self.add().status_code, 400)
        self.client.logout()
        self.assertEqual(self.add().status_code, 302)
