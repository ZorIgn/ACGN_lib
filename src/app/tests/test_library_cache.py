"""Persistent metadata, offline recommendation snapshots and bounded refreshes."""

from datetime import timedelta
from threading import Event
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import models
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from app import library_cache, recommendations
from app.library import model_for
from app.models import Book, Item, LibraryCacheEntry, LibraryRecommendationDismissal
from app.providers import discovery, services
from app.tests.test_recommendations import document


def expire_snapshots():
    LibraryCacheEntry.objects.update(updated_at=timezone.now() - timedelta(hours=2))
    cache.clear()


class MetadataSnapshotTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_uncached_network_failure_keeps_personal_record_editable(self):
        user = get_user_model().objects.create_user(username="offline-detail")
        self.client.force_login(user)
        for kind, failure in [
            ("movie", requests.Timeout),
            ("tv", requests.ConnectionError),
        ]:
            with self.subTest(kind=kind):
                cache.clear()
                item = Item.objects.create(
                    source="tmdb", media_type=kind, media_id="300", title="已收藏的作品"
                )
                record = model_for(kind)(
                    user=user, item=item, score=8, notes="个人记录"
                )
                models.Model.save(record)
                with patch(
                    "app.library.services.get_media_metadata", side_effect=failure
                ):
                    response = self.client.get(f"/library/{kind}/{record.pk}/")
                    self.assertContains(response, "你的记录仍可编辑")
                    self.assertContains(response, 'name="score"')
                    saved = self.client.post(
                        f"/library/{kind}/{record.pk}/",
                        {"score": 9, "status": "Completed", "notes": "离线编辑"},
                    )
                    self.assertEqual(saved.status_code, 302)
                record.refresh_from_db()
                self.assertEqual(record.score, 9)
                self.assertEqual(record.notes, "离线编辑")

    def test_metadata_survives_memory_cache_clear_and_returns_independent_values(self):
        load = Mock(return_value={"title": "作品", "tags": ["冒险"]})
        first = library_cache.get_or_load("detail:book:1", load)
        first["tags"].append("本地修改")
        cache.clear()
        restored = library_cache.get_or_load("detail:book:1", load)
        self.assertEqual(restored, {"title": "作品", "tags": ["冒险"]})
        load.assert_called_once()

    def test_expired_metadata_is_immediate_and_background_success_replaces_snapshot(
        self,
    ):
        library_cache.write("detail:book:1", {"title": "已有资料"})
        expire_snapshots()
        load = Mock(return_value={"title": "更新资料"})
        with patch.object(library_cache, "schedule", return_value=True) as schedule:
            result = library_cache.get_or_load("detail:book:1", load)
        self.assertEqual(result["title"], "已有资料")
        load.assert_not_called()
        schedule.call_args.args[1]()
        self.assertEqual(
            library_cache.read("detail:book:1")["payload"]["title"], "更新资料"
        )

    def test_failed_refresh_keeps_successful_timestamp_and_uses_retry_interval(self):
        library_cache.write("detail:book:1", {"title": "已有资料"})
        expire_snapshots()
        saved_at = LibraryCacheEntry.objects.get().updated_at
        load = Mock(side_effect=requests.Timeout)
        for _ in range(2):
            with library_cache.refresh_context(synchronous=True) as state:
                result = library_cache.get_or_load("detail:book:1", load)
            self.assertTrue(state["stale"])
            self.assertEqual(result["title"], "已有资料")
        load.assert_called_once()
        self.assertEqual(LibraryCacheEntry.objects.get().updated_at, saved_at)

    def test_provider_api_failures_retain_metadata_and_explicit_retry_recovers(self):
        library_cache.write("detail:book:1", {"title": "已有资料"})
        expire_snapshots()
        saved_at = LibraryCacheEntry.objects.get().updated_at
        load = Mock(
            side_effect=services.ProviderAPIError("bangumi", requests.Timeout())
        )
        with library_cache.refresh_context(synchronous=True):
            self.assertEqual(
                library_cache.get_or_load("detail:book:1", load)["title"], "已有资料"
            )
            library_cache.get_or_load("detail:book:1", load)
        load.assert_called_once()
        self.assertEqual(LibraryCacheEntry.objects.get().updated_at, saved_at)
        load.side_effect = None
        load.return_value = {"title": "恢复后的资料"}
        with library_cache.refresh_context(synchronous=True, retry=True):
            self.assertEqual(
                library_cache.get_or_load("detail:book:1", load)["title"],
                "恢复后的资料",
            )
        self.assertEqual(load.call_count, 2)
        self.assertFalse(library_cache.cooling_down("detail:book:1"))

    def test_cold_provider_failure_is_not_saved_and_uses_retry_interval(self):
        load = Mock(
            side_effect=services.ProviderAPIError("bangumi", requests.Timeout())
        )
        with self.assertRaises(services.ProviderAPIError):
            library_cache.get_or_load("detail:book:1", load)
        with self.assertRaises(library_cache.Unavailable):
            library_cache.get_or_load("detail:book:1", load)
        load.assert_called_once()
        self.assertFalse(LibraryCacheEntry.objects.exists())

    def test_malformed_catalog_response_does_not_replace_saved_public_data(self):
        candidates = [document(1, "已有作品", ["科幻"])]
        library_cache.write("discovery:bangumi:book:", candidates)
        expire_snapshots()
        saved_at = LibraryCacheEntry.objects.get().updated_at
        with (
            patch.object(
                discovery, "public_json", return_value={"error": "unavailable"}
            ),
            library_cache.refresh_context(synchronous=True) as state,
        ):
            self.assertEqual(discovery.bangumi_catalog("book"), candidates)
        self.assertTrue(state["stale"])
        self.assertEqual(LibraryCacheEntry.objects.get().updated_at, saved_at)

    def test_public_and_private_snapshots_use_separate_addresses(self):
        users = [
            get_user_model().objects.create_user(username=name) for name in ["甲", "乙"]
        ]
        for owner, title in [
            (None, "公开"),
            (users[0], "甲私藏"),
            (users[1], "乙私藏"),
        ]:
            library_cache.write("same-key", {"title": title}, owner=owner)
        cache.clear()
        for owner, title in [
            (None, "公开"),
            (users[0], "甲私藏"),
            (users[1], "乙私藏"),
        ]:
            self.assertEqual(
                library_cache.read("same-key", owner=owner)["payload"]["title"], title
            )
        self.assertEqual(LibraryCacheEntry.objects.count(), 3)

    def test_public_cache_retention_is_bounded_without_deleting_private_batches(self):
        user = get_user_model().objects.create_user(username="private")
        library_cache.write("recommendations:book:personal", {"items": []}, owner=user)
        with patch.object(library_cache, "PUBLIC_ENTRY_LIMIT", 3):
            for number in range(5):
                library_cache.write(f"detail:book:{number}", {"title": str(number)})
        self.assertEqual(LibraryCacheEntry.objects.filter(owner=None).count(), 3)
        self.assertIsNone(library_cache.read("detail:book:0"))
        self.assertIsNotNone(
            library_cache.read("recommendations:book:personal", owner=user)
        )

    def test_public_catalog_is_bounded_and_does_not_store_chapter_text(self):
        payload = {
            "data": {
                "channel": {
                    "chart": {
                        "bookList": [
                            {
                                "bid": i,
                                "title": f"作品{i}",
                                "intro": "简介",
                                "firstChapterContent": "正文不缓存",
                            }
                            for i in range(120)
                        ]
                    }
                }
            }
        }
        with patch.object(discovery, "public_json", return_value=payload) as source:
            self.assertEqual(len(discovery.novel_catalog()), 100)
            cache.clear()
            self.assertEqual(len(discovery.novel_catalog()), 100)
        source.assert_called_once()
        self.assertNotIn("正文不缓存", str(LibraryCacheEntry.objects.get().payload))


class RecommendationSnapshotTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="cache-owner")
        self.other = get_user_model().objects.create_user(username="cache-other")
        self.catalog = [
            {**document(i, f"作品 {i}", ["科幻"]), "reason": "相似题材"}
            for i in range(80)
        ]
        ranker = patch.object(
            recommendations,
            "recommend",
            return_value={"items": self.catalog, "personalized": True},
        )
        self.ranker = ranker.start()
        self.addCleanup(ranker.stop)
        scheduler = patch.object(library_cache, "schedule", return_value=False)
        self.scheduler = scheduler.start()
        self.addCleanup(scheduler.stop)

    def add(self, media_id, score=None):
        item = Item.objects.create(
            source="bangumi",
            media_type="book",
            media_id=str(media_id),
            title=f"作品 {media_id}",
        )
        record = Book(user=self.user, item=item, score=score)
        models.Model.save(record)
        return record

    def test_restart_restores_ranked_pages_without_provider_calls_and_is_owner_bound(
        self,
    ):
        first = recommendations.batch(self.user, "book")
        cache.clear()
        self.assertEqual(recommendations.batch(self.user, "book"), first)
        self.assertEqual(self.ranker.call_count, 1)
        self.ranker.return_value = {
            "items": list(reversed(self.catalog)),
            "personalized": True,
        }
        other = recommendations.batch(self.other, "book")
        self.assertNotEqual(
            other["items"][0]["media_id"], first["items"][0]["media_id"]
        )
        cache.clear()
        self.assertEqual(recommendations.batch(self.user, "book"), first)
        self.assertEqual(self.ranker.call_count, 2)
        self.assertEqual(LibraryCacheEntry.objects.filter(owner=None).count(), 0)

    def test_expired_batch_is_shown_before_background_refresh_and_new_results_are_visible(
        self,
    ):
        first = recommendations.batch(self.user, "book")
        expire_snapshots()
        self.scheduler.return_value = True
        stale = recommendations.batch(self.user, "book")
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["refreshing"])
        self.assertEqual(stale["items"], first["items"])
        self.assertEqual(self.ranker.call_count, 1)
        self.ranker.return_value = {
            "items": list(reversed(self.catalog)),
            "personalized": True,
        }
        self.scheduler.call_args.args[1]()
        updated = recommendations.batch(self.user, "book")
        self.assertFalse(updated["stale"])
        self.assertFalse(updated["refreshing"])
        self.assertEqual(updated["items"][0]["media_id"], "79")
        self.assertTrue(self.ranker.call_args.kwargs["refresh"])

    def test_failed_refresh_preserves_saved_batch_and_save_time(self):
        first = recommendations.batch(self.user, "book")
        expire_snapshots()
        saved_at = LibraryCacheEntry.objects.get().updated_at
        self.scheduler.return_value = True
        recommendations.batch(self.user, "book")
        self.ranker.return_value = {"items": [], "unavailable": True}
        with self.assertRaises(library_cache.Unavailable):
            self.scheduler.call_args.args[1]()
        self.assertEqual(LibraryCacheEntry.objects.get().updated_at, saved_at)
        self.assertEqual(
            recommendations.batch(self.user, "book")["items"], first["items"]
        )

    def test_offline_add_and_dismissal_are_excluded_and_fill_only_their_slots(self):
        before = recommendations.batch(self.user, "book")["items"]
        cache.clear()
        self.add(7)
        LibraryRecommendationDismissal.objects.create(
            user=self.user,
            source="bangumi",
            media_type="book",
            media_id="8",
            title="作品 8",
        )
        self.ranker.return_value = {"items": [], "unavailable": True}
        result = recommendations.batch(self.user, "book")
        self.assertTrue(result["stale"])
        after = result["items"]
        self.assertEqual(len(after), 50)
        self.assertEqual([after[7]["media_id"], after[8]["media_id"]], ["50", "51"])
        for index in range(50):
            if index not in {7, 8}:
                self.assertEqual(after[index]["media_id"], before[index]["media_id"])
        cache.clear()
        self.assertEqual(recommendations.batch(self.user, "book")["items"], after)

    def test_score_change_after_restart_rebuilds_even_with_fresh_snapshot(self):
        record = self.add(79, score=9)
        recommendations.batch(self.user, "book")
        cache.clear()
        recommendations.batch(self.user, "book")
        self.assertEqual(self.ranker.call_count, 1)
        Book.objects.filter(pk=record.pk).update(score=2)
        self.ranker.return_value = {
            "items": list(reversed(self.catalog)),
            "personalized": True,
        }
        result = recommendations.batch(self.user, "book")
        self.assertEqual(self.ranker.call_count, 2)
        self.assertEqual(result["items"][0]["media_id"], "78")


class RefreshQueueTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_queue_is_bounded_deduplicated_and_cools_down_failures(self):
        pending = set()
        with (
            patch.object(library_cache, "_pending", pending),
            patch.object(library_cache._executor, "submit") as submit,
            patch.object(library_cache, "close_old_connections") as open_connections,
            patch.object(library_cache.connections, "close_all") as close_connections,
        ):
            failure = Mock(side_effect=requests.Timeout)
            self.assertTrue(library_cache.schedule("one", failure))
            self.assertTrue(library_cache.schedule("one", failure))
            self.assertEqual(submit.call_count, 1)
            for i in range(7):
                self.assertTrue(library_cache.schedule(str(i), Mock()))
            self.assertFalse(library_cache.schedule("overflow", Mock()))
            submit.call_args_list[0].args[0]()
            self.assertEqual(len(pending), 7)
            self.assertFalse(library_cache.schedule("one", failure))
            open_connections.assert_called_once()
            close_connections.assert_called_once()


class OfflinePublicCatalogTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="offline-owner")

    def test_expired_public_catalog_can_rank_changed_preferences_while_offline(self):
        candidates = [document(i, f"科幻作品 {i}", ["科幻"]) for i in range(80)]
        library_cache.write("discovery:bangumi:book:", candidates)
        library_cache.write("discovery:webnovel:", [])
        with patch.object(library_cache, "schedule", return_value=False):
            original = recommendations.batch(self.user, "book")
            self.assertEqual(len(original["items"]), 50)
            expire_snapshots()
            LibraryRecommendationDismissal.objects.create(
                user=self.user,
                source="bangumi",
                media_type="book",
                media_id="7",
                title="科幻作品 7",
            )
            with patch.object(
                discovery, "public_json", side_effect=requests.Timeout
            ) as network:
                changed = recommendations.batch(self.user, "book")
                self.assertEqual(len(changed["items"]), 50)
                self.assertNotIn("7", [item["media_id"] for item in changed["items"]])
                self.assertTrue(changed["stale"])
                network.assert_not_called()
                refreshed = recommendations.recommend(self.user, "book", refresh=True)
                self.assertEqual(len(refreshed["items"]), 50)
                self.assertTrue(refreshed["stale"])
                self.assertFalse(refreshed["unavailable"])


class BackgroundRefreshTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="refresh-owner")
        self.jobs = []
        submit = library_cache._executor.submit

        def record_job(*args, **kwargs):
            future = submit(*args, **kwargs)
            self.jobs.append(future)
            return future

        patcher = patch.object(
            library_cache._executor, "submit", side_effect=record_job
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for future in self.jobs:
            future.result(timeout=10)

    def test_actual_worker_keeps_stale_metadata_available_until_refresh_finishes(self):
        key = "detail:book:worker"
        library_cache.write(key, {"title": "已有资料"})
        expire_snapshots()
        started, release = Event(), Event()

        def load():
            started.set()
            if not release.wait(5):
                raise AssertionError("Background loader was not released")
            return {"title": "更新资料"}

        try:
            self.assertEqual(library_cache.get_or_load(key, load)["title"], "已有资料")
            self.assertTrue(started.wait(5))
            self.assertTrue(library_cache.refreshing(key))
            self.assertEqual(library_cache.get_or_load(key, load)["title"], "已有资料")
            self.assertEqual(len(self.jobs), 1)
        finally:
            release.set()
        self.jobs[0].result(timeout=5)
        self.assertFalse(library_cache.refreshing(key))
        cache.clear()
        self.assertEqual(library_cache.read(key)["payload"]["title"], "更新资料")

    def test_actual_provider_failure_cools_down_and_retry_keeps_queue_deduplicated(
        self,
    ):
        key = "detail:book:failed-worker"
        library_cache.write(key, {"title": "已有资料"})
        expire_snapshots()
        saved_at = LibraryCacheEntry.objects.get().updated_at
        load = Mock(
            side_effect=services.ProviderAPIError("bangumi", requests.Timeout())
        )
        self.assertEqual(library_cache.get_or_load(key, load)["title"], "已有资料")
        self.jobs[0].result(timeout=5)
        self.assertFalse(library_cache.refreshing(key))
        self.assertTrue(library_cache.cooling_down(key))
        self.assertEqual(LibraryCacheEntry.objects.get().updated_at, saved_at)
        library_cache.get_or_load(key, load)
        self.assertEqual(len(self.jobs), 1)
        load.assert_called_once()
        load.side_effect = None
        load.return_value = {"title": "联网后的资料"}
        cache.touch(library_cache.storage_key(key) + ":retry", timeout=-1)
        self.assertEqual(library_cache.get_or_load(key, load)["title"], "已有资料")
        self.jobs[1].result(timeout=5)
        self.assertEqual(library_cache.read(key)["payload"]["title"], "联网后的资料")
        self.assertFalse(library_cache.cooling_down(key))

    def test_explicit_batch_retry_stays_refreshing_until_new_snapshot_is_ready(self):
        candidates = [document(i, f"作品 {i}", ["科幻"]) for i in range(80)]
        key = "recommendations:book:personal"
        with patch.object(
            recommendations, "recommend", return_value={"items": candidates}
        ):
            initial = recommendations.batch(self.user, "book")
        cache.set(library_cache.storage_key(key, self.user) + ":retry", 1, 300)
        started, release = Event(), Event()

        def load(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError("Background ranking was not released")
            return {"items": list(reversed(candidates))}

        with patch.object(recommendations, "recommend", side_effect=load) as ranker:
            try:
                pending = recommendations.batch(self.user, "book", refresh=True)
                self.assertTrue(pending["refreshing"])
                self.assertEqual(pending["items"], initial["items"])
                self.assertTrue(started.wait(5))
                polling = recommendations.batch(self.user, "book")
                self.assertTrue(polling["refreshing"])
                self.assertEqual(polling["items"], initial["items"])
                self.assertEqual(len(self.jobs), 1)
            finally:
                release.set()
            self.jobs[0].result(timeout=5)
            updated = recommendations.batch(self.user, "book")
        self.assertFalse(updated["refreshing"])
        self.assertFalse(updated["stale"])
        self.assertEqual(updated["items"][0]["media_id"], "79")
        self.assertTrue(ranker.call_args.kwargs["retry"])
        self.assertFalse(library_cache.cooling_down(key, owner=self.user))

    def test_background_result_cannot_restore_a_work_dismissed_during_refresh(self):
        candidates = [document(i, f"作品 {i}", ["科幻"]) for i in range(80)]
        with patch.object(
            recommendations, "recommend", return_value={"items": candidates}
        ):
            recommendations.batch(self.user, "book")
        expire_snapshots()
        started, release = Event(), Event()

        def load(*args, **kwargs):
            if kwargs.get("refresh"):
                started.set()
                if not release.wait(5):
                    raise AssertionError("Background ranking was not released")
                return {"items": list(reversed(candidates))}
            return {"items": candidates}

        with patch.object(recommendations, "recommend", side_effect=load):
            try:
                recommendations.batch(self.user, "book")
                self.assertTrue(started.wait(5))
                LibraryRecommendationDismissal.objects.create(
                    user=self.user,
                    source="bangumi",
                    media_type="book",
                    media_id="7",
                    title="作品 7",
                )
                changed = recommendations.batch(self.user, "book")
                self.assertEqual(changed["items"][7]["media_id"], "50")
                self.assertTrue(changed["refreshing"])
            finally:
                release.set()
            self.jobs[0].result(timeout=5)
        cache.clear()
        after = recommendations.batch(self.user, "book")
        self.assertEqual(after["items"], changed["items"])
        self.assertNotIn("7", [item["media_id"] for item in after["items"]])
        self.assertFalse(after["refreshing"])
