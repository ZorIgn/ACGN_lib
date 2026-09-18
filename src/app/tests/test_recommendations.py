"""Taste ranking, provider failures and private recommendation review."""

from types import SimpleNamespace
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.db import models
from django.test import SimpleTestCase, TestCase

from app import library_discovery, recommendations as rec
from app.models import Book, Item, LibraryImportDraft
from app.providers import discovery


def document(media_id, title, tags, heat=100):
    return {
        "source": "bangumi",
        "media_type": "book",
        "media_id": str(media_id),
        "title": title,
        "original_title": "",
        "tags": tags,
        "summary": "",
        "author": "",
        "image": "/static/img/none.svg",
        "heat": heat,
        "community_score": 8,
        "votes": 100,
        "source_label": "Bangumi",
    }


class RankingTests(SimpleTestCase):
    def test_rating_preference_outweighs_popularity_and_low_rated_content(self):
        fantasy = document(1, "仙道旅途", ["仙侠", "修仙"], 10)
        romance = document(2, "校园之恋", ["校园", "恋爱"], 100000)
        seeds = [
            {
                **document(3, "喜欢的小说", ["仙侠", "修仙"]),
                "weight": 1,
                "personal_score": 10,
            },
            {
                **document(4, "不喜欢的小说", ["校园", "恋爱"]),
                "weight": -1,
                "personal_score": 0,
            },
        ]
        result, personalized = rec.rank([romance, fantasy], seeds, [])
        self.assertEqual(result[0]["title"], "仙道旅途")
        self.assertTrue(personalized)
        self.assertIn("10 分", result[0]["reason"])
        self.assertNotIn("不喜欢", result[0]["reason"])
        seeds[0]["weight"], seeds[1]["weight"] = -1, 1
        seeds[0]["personal_score"], seeds[1]["personal_score"] = 0, 10
        self.assertEqual(
            rec.rank([romance, fantasy], seeds, [])[0][0]["title"], "校园之恋"
        )

    def test_popular_mode_and_cold_start_follow_public_heat(self):
        candidates = [
            document(1, "作品一", ["冒险"], 10),
            document(2, "作品二", ["恋爱"], 10000),
        ]
        result, personalized = rec.rank(candidates, [], [], personal=False)
        self.assertEqual(result[0]["title"], "作品二")
        self.assertFalse(personalized)
        self.assertEqual(result[0]["reason"], "Bangumi 热门作品")

    def test_owned_aliases_and_duplicate_sources_are_excluded(self):
        owned = [
            SimpleNamespace(
                item=SimpleNamespace(source="steam", media_id="99", title="Hades II")
            )
        ]
        candidates = [
            {**document(1, "哈迪斯 2", []), "original_title": "Hades II"},
            document(2, "未读作品", []),
            {**document(3, "未读作品", []), "source": "webnovel"},
        ]
        result, _ = rec.rank(candidates, [], owned)
        self.assertEqual([item["title"] for item in result], ["未读作品"])

    def test_diversity_can_promote_a_different_topic_between_similar_items(self):
        candidates = [
            document(1, "一", ["修仙"], 100),
            document(2, "二", ["修仙"], 100),
            document(3, "三", ["推理"], 100),
        ]
        seeds = [
            {**document(4, "甲", ["修仙"]), "weight": 1, "personal_score": 10},
            {**document(5, "乙", ["推理"]), "weight": 1, "personal_score": 10},
        ]
        result, _ = rec.rank(candidates, seeds, [])
        self.assertNotEqual(result[0]["tags"], result[1]["tags"])

    def test_feedback_respects_zero_and_caps_playtime(self):
        record = SimpleNamespace(
            score=0,
            status="In progress",
            progress=9000000,
            item=SimpleNamespace(media_type="game"),
        )
        self.assertEqual(rec.feedback(record), -1)
        record.score = None
        self.assertLess(rec.feedback(record), 0.5)
        record.status = "Dropped"
        self.assertLess(rec.feedback(record), 0)


class RecommendationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="recommend-owner")
        cls.other = get_user_model().objects.create_user(username="recommend-other")
        for user, title, score in [
            (cls.owner, "仙侠私藏", 10),
            (cls.other, "爱情私藏", 10),
        ]:
            item = Item.objects.create(
                source="bangumi",
                media_type="book",
                media_id=str(user.pk),
                title=title,
                image="/static/img/none.svg",
            )
            models.Model.save(
                Book(
                    user=user,
                    item=item,
                    score=score,
                    status="Completed",
                    notes="私人笔记绝不能发送",
                )
            )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.owner)
        self.catalog = [
            document(100, "山中修行", ["仙侠"], 100),
            document(101, "恋爱日记", ["爱情"], 100),
        ]

    def seed(self, source, kind, media_id, title):
        return document(media_id, title, ["仙侠" if title == "仙侠私藏" else "爱情"])

    def test_only_current_owner_affects_results_and_rating_edits_take_effect(self):
        with (
            patch.object(discovery, "bangumi_catalog", return_value=self.catalog),
            patch.object(discovery, "novel_catalog", return_value=[]),
            patch.object(discovery, "describe", side_effect=self.seed) as describe,
        ):
            first = self.client.get("/library/recommendations/", {"type": "book"})
            self.assertEqual(first.context["items"][0]["title"], "山中修行")
            self.assertNotContains(first, "爱情私藏")
            self.assertIn("no-store", first.headers["Cache-Control"])
            Book.objects.filter(user=self.owner).update(score=0)
            second = self.client.get("/library/recommendations/", {"type": "book"})
            self.assertEqual(second.context["items"][0]["title"], "恋爱日记")
            self.assertNotIn("私人笔记", str(describe.call_args_list))
            self.client.force_login(self.other)
            third = self.client.get("/library/recommendations/", {"type": "book"})
            self.assertEqual(third.context["items"][0]["title"], "恋爱日记")
            self.assertNotContains(third, "仙侠私藏")

    def test_unavailable_sources_keep_the_search_page_usable(self):
        with (
            patch.object(discovery, "bangumi_catalog", side_effect=requests.Timeout),
            patch.object(discovery, "novel_catalog", side_effect=requests.Timeout),
        ):
            response = self.client.get(
                "/library/recommendations/", {"type": "book", "mode": "popular"}
            )
            self.assertContains(response, "暂时无法获取推荐资料")
        with (
            patch.object(discovery, "bangumi_catalog", side_effect=requests.Timeout),
            patch.object(discovery, "novel_catalog", return_value=self.catalog),
        ):
            response = self.client.get(
                "/library/recommendations/", {"type": "book", "mode": "popular"}
            )
            self.assertEqual(len(response.context["items"]), 2)
            self.assertContains(response, "部分资料源暂时不可用")
        with patch.object(
            rec,
            "recommend",
            side_effect=AssertionError("search page must not wait for recommendations"),
        ):
            self.assertEqual(self.client.get("/library/add/").status_code, 200)

    def test_verified_recommendation_opens_review_without_importing_automatically(self):
        with patch.object(
            rec,
            "recommend",
            return_value={
                "items": [{**self.catalog[0], "reason": "Bangumi 热门作品"}],
                "personalized": False,
            },
        ):
            response = self.client.get("/library/recommendations/")
        token = BeautifulSoup(response.content, "html.parser").select_one(
            'input[name="candidate"]'
        )["value"]
        response = self.client.post(
            "/library/recommendations/choose/", {"candidate": token}
        )
        self.assertEqual(response.status_code, 302)
        draft = LibraryImportDraft.objects.get(user=self.owner)
        self.assertEqual(draft.entries[0]["selected"], [0])
        self.assertEqual(Book.objects.filter(user=self.owner).count(), 1)
        review = self.client.get(response.url)
        self.assertContains(review, "山中修行")
        self.assertEqual(
            self.client.post(
                "/library/add/",
                {
                    "action": "import",
                    "draft_id": draft.pk,
                    "page": 1,
                    "choice_0": 0,
                    "0-0-status": "Planning",
                },
            ).status_code,
            302,
        )
        self.assertEqual(Book.objects.filter(user=self.owner).count(), 2)
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(
                "/library/recommendations/choose/", {"candidate": token}
            ).status_code,
            400,
        )

    def test_authentication_validation_and_signature_are_required(self):
        self.assertEqual(
            self.client.get(
                "/library/recommendations/", {"type": "invalid"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/library/recommendations/", {"mode": "invalid"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/library/recommendations/choose/", {"candidate": "tampered"}
            ).status_code,
            400,
        )
        with patch("django.core.signing.time.time", return_value=1):
            token = signing.dumps(
                {"user": self.owner.pk, "candidate": self.catalog[0]},
                salt=library_discovery.TOKEN_SALT,
            )
        self.assertEqual(
            self.client.post(
                "/library/recommendations/choose/", {"candidate": token}
            ).status_code,
            400,
        )
        self.client.logout()
        self.assertEqual(self.client.get("/library/recommendations/").status_code, 302)
        self.assertEqual(
            self.client.post("/library/recommendations/choose/", {}).status_code, 302
        )


class DiscoveryProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_novel_catalog_keeps_chart_rank_and_caches_public_data(self):
        data = {
            "data": {
                "1": {
                    "chart": {
                        "bookList": [
                            {
                                "bid": 12,
                                "title": "书",
                                "author": "作者",
                                "intro": "简介",
                                "readingNum": 123,
                            }
                        ]
                    }
                }
            }
        }
        with patch.object(discovery, "public_json", return_value=data) as request:
            for _ in range(2):
                rows = discovery.novel_catalog()
            request.assert_called_once()
        self.assertEqual(rows[0]["chart_position"], 1)
        self.assertEqual(rows[0]["heat"], 123)

    def test_bangumi_pool_separates_novels_from_manga(self):
        rows = [
            {"id": 1, "name": "小说", "type": 1, "platform": "小说"},
            {"id": 2, "name": "漫画", "type": 1, "platform": "漫画"},
        ]
        with patch.object(
            discovery, "public_json", return_value={"data": rows}
        ) as request:
            result = discovery.bangumi_catalog("book", "仙侠")
        self.assertEqual(len(result), 1)
        self.assertEqual(
            request.call_args.kwargs["payload"]["filter"]["tag"], ["小说", "仙侠"]
        )

    def test_seed_document_excludes_chapter_content(self):
        with patch.object(
            discovery,
            "public_json",
            return_value={
                "data": {
                    "bid": 12,
                    "title": "书",
                    "intro": "简介",
                    "firstChapterContent": "不应保留的章节",
                }
            },
        ):
            doc = discovery.describe("webnovel", "book", "12", "书")
        self.assertNotIn("firstChapterContent", doc)
        self.assertNotIn("不应保留", str(doc))
