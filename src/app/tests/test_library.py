"""Behavior checks for private imports, metadata and Steam updates."""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import models
from django.test import TestCase

from app import library
from app.library_tasks import sync_steam
from app.models import (
    Book,
    Game,
    Item,
    LibraryImportDraft,
    LibraryLink,
    Sources,
    SteamConnection,
)
from app.providers import bangumi
from integrations.imports.helpers import encrypt


class LibraryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="library-test-owner", password="test-secret-safe"
        )
        cls.other = get_user_model().objects.create_user(
            username="library-test-other", password="other-secret-safe"
        )

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()
        covers = patch("app.library_tasks.steam.covers", return_value={})
        covers.start()
        self.addCleanup(covers.stop)

    def candidate(self):
        return {
            "source": "manual",
            "media_type": "book",
            "title": "测试作品",
            "image": "/static/img/none.svg",
        }

    def payload(self, score="", status="Planning"):
        candidate = self.candidate()
        row = {
            "input": candidate["title"],
            "media_type": "book",
            "notes": "很喜欢",
            "viewing_url": "",
            "choices": [],
            "manual_title": candidate["title"],
            "manual": candidate,
        }
        draft = LibraryImportDraft.objects.create(
            user=self.user, title="测试清单", entries=[row]
        )
        return {
            "action": "import",
            "draft_id": str(draft.pk),
            "page": "1",
            "choice_0": "0",
            "manual_title_0": candidate["title"],
            "0-0-status": status,
            "0-0-score": score,
            "0-0-notes": "很喜欢",
            "0-0-viewing_url": "https://example.org/read/1",
        }

    def test_import_allows_blank_status_and_score(self):
        response = self.client.post("/library/add/", self.payload(status=""))
        self.assertEqual(response.status_code, 302)
        record = Book.objects.get()
        self.assertEqual(record.status, "")
        self.assertIsNone(record.score)
        self.assertEqual(record.notes, "很喜欢")

    def test_duplicate_import_keeps_personal_data_and_unknown_dates(self):
        payload = self.payload(score="10", status="Completed")
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 302)
        payload["0-0-score"] = "2"
        self.client.post("/library/add/", payload)
        record = Book.objects.get()
        self.assertEqual(record.score, Decimal("10"))
        self.assertEqual(record.notes, "很喜欢")
        self.assertIsNone(record.end_date)
        self.assertEqual(record.progress, 0)
        self.assertEqual(LibraryLink.objects.get().user, self.user)

    def test_blank_and_zero_score_are_distinct_and_private(self):
        self.client.post("/library/add/", self.payload())
        record = Book.objects.get()
        self.assertIsNone(record.score)
        response = self.client.post(
            f"/library/book/{record.pk}/",
            {"score": "0", "status": "Paused", "notes": "保留记录", "viewing_url": ""},
        )
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.score, Decimal("0"))
        self.assertFalse(LibraryLink.objects.exists())
        self.assertContains(self.client.get("/library/"), "测试作品")
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(f"/library/book/{record.pk}/").status_code, 404
        )
        self.assertNotContains(self.client.get("/library/"), "测试作品")
        self.client.logout()
        self.assertEqual(self.client.get("/library/").status_code, 302)

    def test_invalid_score_and_url_are_rejected(self):
        payload = self.payload(score="10.1")
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 400)
        payload["0-0-score"] = "0"
        payload["0-0-viewing_url"] = "ftp://example.org/a"
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 400)
        self.assertFalse(Book.objects.exists())

    def multi_payload(self):
        row = {
            "input": "哈迪斯",
            "media_type": "game",
            "notes": "原话",
            "choices": [
                {
                    "source": "bangumi",
                    "media_id": str(index),
                    "media_type": "game",
                    "title": title,
                    "image": "/static/img/none.svg",
                }
                for index, title in [(1, "哈迪斯"), (2, "哈迪斯 2")]
            ],
            "viewing_url": "",
            "manual_title": "哈迪斯",
            "manual": self.candidate(),
        }
        draft = LibraryImportDraft.objects.create(
            user=self.user, title="哈迪斯清单", entries=[row]
        )
        return {
            "action": "import",
            "draft_id": str(draft.pk),
            "page": "1",
            "choice_0": ["0", "1"],
            "0-0-score": "9",
            "0-0-status": "Completed",
            "0-0-notes": "喜欢一代",
            "0-0-viewing_url": "https://example.org/one",
            "0-1-score": "6",
            "0-1-status": "",
            "0-1-status_manual": "1",
            "0-1-notes": "二代尚未玩完",
            "0-1-viewing_url": "https://example.org/two",
        }

    def test_multiple_candidates_have_independent_records(self):
        payload = self.multi_payload()
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 302)
        first, second = [
            Game.objects.get(item__media_id=str(index)) for index in (1, 2)
        ]
        self.assertEqual(
            (first.score, first.status, first.notes),
            (Decimal("9"), "Completed", "喜欢一代"),
        )
        self.assertEqual(
            (second.score, second.status, second.notes),
            (Decimal("6"), "", "二代尚未玩完"),
        )
        self.assertEqual(
            LibraryLink.objects.get(item=second.item).url, "https://example.org/two"
        )
        self.client.post("/library/add/", payload)
        self.assertEqual(Game.objects.count(), 2)

    def test_invalid_candidate_preserves_each_edit_and_saves_nothing(self):
        payload = self.multi_payload()
        payload["0-1-score"] = "11"
        response = self.client.post("/library/add/", payload)
        self.assertEqual(response.status_code, 400)
        options = response.context["rows"][0]["options"]
        self.assertTrue(options[0]["selected"] and options[1]["selected"])
        self.assertEqual(options[0]["personal"]["score"].value(), "9")
        self.assertEqual(options[1]["personal"]["notes"].value(), "二代尚未玩完")
        self.assertEqual(options[1]["personal"]["score"].value(), "11")
        self.assertFalse(Game.objects.exists())

    def test_unselected_candidate_fields_are_ignored(self):
        payload = self.multi_payload()
        payload["choice_0"] = payload["choice_0"][:1]
        payload["0-1-score"] = "11"
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 302)
        self.assertEqual(
            list(Game.objects.values_list("item__title", flat=True)), ["哈迪斯"]
        )

    def test_all_candidates_can_be_unchecked(self):
        payload = self.multi_payload()
        payload.pop("choice_0")
        response = self.client.post("/library/add/", payload)
        self.assertContains(response, "请选择要加入书架的作品", status_code=400)
        self.assertFalse(Game.objects.exists())

    def test_status_and_score_can_be_cleared_in_detail(self):
        self.client.post("/library/add/", self.payload(score="8.5", status="Completed"))
        record = Book.objects.get()
        self.client.post(
            f"/library/book/{record.pk}/",
            {
                "score": "",
                "status": "",
                "notes": "",
                "viewing_url": "",
            },
        )
        record.refresh_from_db()
        self.assertEqual(record.status, "")
        self.assertIsNone(record.score)

    def test_untrusted_candidate_does_not_create_record(self):
        payload = self.payload()
        payload["choice_0"] += "tampered"
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 400)
        self.assertFalse(Book.objects.exists())

    def test_rating_defaults_to_completed_and_respects_manual_status(self):
        self.client.post("/library/add/", self.payload(score="9", status=""))
        record = Book.objects.get()
        self.assertEqual(record.status, "Completed")
        self.client.post(
            f"/library/book/{record.pk}/",
            {
                "score": "8",
                "status": "",
                "status_manual": "1",
                "notes": "",
                "viewing_url": "",
            },
        )
        record.refresh_from_db()
        self.assertEqual(record.status, "")

    def test_pages_save_and_import_together(self):
        import copy

        payload = self.payload()
        draft = LibraryImportDraft.objects.get(pk=payload["draft_id"])
        template = draft.entries[0]
        draft.entries = []
        for index in range(21):
            row = copy.deepcopy(template)
            row["input"] = row["manual_title"] = row["manual"]["title"] = f"作品{index}"
            draft.entries.append(row)
        draft.save()
        first = {
            "draft_id": str(draft.pk),
            "action": "draft",
            "page": "1",
            "choice_0": "0",
            "0-0-score": "9",
            "0-0-status": "",
            "0-0-notes": "第一页感想",
        }
        self.assertEqual(
            self.client.post("/library/add/", first).json()["selected_count"], 1
        )
        second = {
            "draft_id": str(draft.pk),
            "action": "draft",
            "page": "2",
            "choice_20": "0",
            "20-0-score": "6",
            "20-0-status": "Paused",
            "20-0-notes": "第二页感想",
        }
        self.assertEqual(
            self.client.post("/library/add/", second).json()["selected_count"], 2
        )
        response = self.client.get(library.draft_url(draft, 1))
        self.assertEqual(
            response.context["rows"][0]["options"][0]["personal"]["notes"].value(),
            "第一页感想",
        )
        self.assertEqual(response.context["draft_selected_count"], 2)
        second["action"] = "import"
        self.assertEqual(self.client.post("/library/add/", second).status_code, 302)
        self.assertEqual(Book.objects.count(), 2)
        self.assertEqual(Book.objects.get(item__title="作品0").notes, "第一页感想")
        self.assertEqual(Book.objects.get(item__title="作品20").status, "Paused")
        draft.refresh_from_db()
        self.assertIsNotNone(draft.imported_at)
        self.assertEqual(len(draft.entries[0]["selected"]), 1)

    def test_drafts_are_private_and_reusable(self):
        payload = self.multi_payload()
        self.client.post("/library/add/", {**payload, "action": "draft"})
        response = self.client.get("/library/add/", {"reuse": payload["draft_id"]})
        self.assertIn(
            "游戏 | 哈迪斯 | 原话", response.context["form"]["entries"].value()
        )
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(
                "/library/add/", {"draft": payload["draft_id"]}
            ).status_code,
            404,
        )
        self.assertEqual(self.client.post("/library/add/", payload).status_code, 404)
        self.assertEqual(
            self.client.get(
                "/library/add/", {"reuse": payload["draft_id"]}
            ).status_code,
            404,
        )

    def test_create_mixed_draft_larger_than_one_page(self):
        entries = [f"小说 | 测试小说{index} | 喜欢" for index in range(22)] + [
            "游戏 | 哈迪斯"
        ]
        response = self.client.post(
            "/library/add/", {"media_type": "book", "entries": "\n".join(entries)}
        )
        self.assertEqual(response.status_code, 302)
        draft = LibraryImportDraft.objects.get(title="测试小说0等 23 部作品")
        self.assertEqual(draft.entries[-1]["media_type"], "game")
        self.assertEqual(draft.entries[0]["notes"], "喜欢")

    def test_unrelated_title_is_not_a_candidate(self):
        self.assertFalse(library.title_matches("诡秘地海", {"title": "诡秘之主"}))
        self.assertTrue(library.title_matches("哈迪斯", {"title": "哈迪斯 2"}))
        self.assertTrue(
            library.title_matches(
                "Hades", {"title": "哈迪斯 2", "original_title": "Hades II"}
            )
        )

    def test_custom_url_is_saved_without_fetching_it(self):
        with patch("app.library.services.search") as search:
            row = library.lookup(
                {"input": "https://127.0.0.1/private", "media_type": "book"}
            )
        search.assert_not_called()
        self.assertEqual(row["viewing_url"], "https://127.0.0.1/private")
        self.assertFalse(row["choices"])

    def test_bangumi_separates_novel_and_manga(self):
        data = {
            "data": [
                {
                    "id": 1,
                    "type": 1,
                    "platform": "小说",
                    "name": "小说",
                    "name_cn": "",
                    "images": {},
                },
                {"id": 2, "type": 1, "platform": "漫画", "name": "漫画", "images": {}},
            ],
            "total": 2,
        }
        with patch("app.providers.bangumi.request", return_value=data):
            results = bangumi.search("book", "测试", 1)
        self.assertEqual([item["media_id"] for item in results["results"]], ["1"])

    def test_signup_closed_after_initial_owner(self):
        self.client.logout()
        response = self.client.get("/accounts/signup/")
        self.assertContains(response, "书架已有主人")

    def test_all_native_pages_render(self):
        for path in ["/library/", "/library/add/", "/library/steam/"]:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_steam_repeated_sync_preserves_personal_decisions(self):
        connection = SteamConnection.objects.create(
            user=self.user,
            steam_id="76561198000000000",
            encrypted_key=encrypt("a" * 32),
        )
        games = [{"appid": 123, "name": "未匹配游戏", "playtime_forever": 150}]
        with patch("app.library_tasks.fetch_games", return_value=games):
            sync_steam(self.user.pk)
        record = Game.objects.get()
        record.score, record.status, record.notes = (
            Decimal("8.5"),
            "Dropped",
            "我的感想",
        )
        models.Model.save(record)
        games[0]["playtime_forever"] = 210
        with patch("app.library_tasks.fetch_games", return_value=games):
            sync_steam(self.user.pk)
            sync_steam(self.user.pk)
        record.refresh_from_db()
        self.assertEqual(Game.objects.count(), 1)
        self.assertEqual(record.item.source, Sources.STEAM)
        self.assertEqual(record.progress, 210)
        self.assertEqual(
            (record.score, record.status, record.notes),
            (Decimal("8.5"), "Dropped", "我的感想"),
        )
        connection.refresh_from_db()
        self.assertFalse(connection.running)
        self.assertIsNotNone(connection.last_synced)

    def test_steam_failed_batch_rolls_back_records(self):
        SteamConnection.objects.create(
            user=self.user,
            steam_id="76561198000000000",
            encrypted_key=encrypt("a" * 32),
        )
        games = [
            {"appid": 123, "name": "正常游戏", "playtime_forever": 150},
            {"appid": "bad"},
        ]
        with patch("app.library_tasks.fetch_games", return_value=games):
            sync_steam(self.user.pk)
        self.assertFalse(Game.objects.exists())
        self.assertFalse(Item.objects.filter(source="steam").exists())

    def test_steam_credentials_are_encrypted_and_not_in_schedule(self):
        key = "a" * 32
        response = self.client.post(
            "/library/steam/",
            {
                "steam_id": "76561198000000000",
                "api_key": key,
                "automatic": "on",
                "action": "save",
            },
        )
        self.assertEqual(response.status_code, 302)
        saved = SteamConnection.objects.get(user=self.user)
        self.assertNotEqual(saved.encrypted_key, key)
        from django_celery_beat.models import PeriodicTask

        task = PeriodicTask.objects.get(name=f"library-steam-{self.user.pk}")
        self.assertNotIn(key, task.args + task.kwargs)
        self.assertNotContains(self.client.get("/library/steam/"), key)
