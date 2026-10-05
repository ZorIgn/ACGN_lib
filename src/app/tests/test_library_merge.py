"""A reviewed duplicate merge preserves records, history and private ownership."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import DatabaseError, models
from django.test import Client, TestCase
from django.utils import timezone

from app import library_merge
from app.library import model_for
from app.library_tasks import sync_steam
from app.models import (
    Book,
    Game,
    Item,
    LibraryFolder,
    LibraryLink,
    LibrarySeries,
    LibrarySeriesMember,
    Season,
    SteamConnection,
)


class MergeTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="merge-owner")
        self.other = get_user_model().objects.create_user(username="merge-other")
        self.client.force_login(self.owner)
        self.source = self.record(
            "source", notes="相同笔记\n\n来源笔记", score=8, status="Completed"
        )
        self.target = self.record("target", notes="相同笔记", score=0, status="")

    def record(self, name, kind="book", owner=None, item=None, **fields):
        item = item or Item.objects.create(
            source="manual",
            media_type=kind,
            media_id=name,
            title=name,
            image="/static/none.svg",
        )
        row = model_for(kind)(user=owner or self.owner, item=item, **fields)
        models.Model.save(row)
        return row

    def preview(self, source=None, target=None, **choices):
        source, target = source or self.source, target or self.target
        return self.client.post(
            "/library/merge/",
            {
                "action": "preview",
                "source": f"{source.item.media_type}:{source.pk}",
                "target": f"{target.item.media_type}:{target.pk}",
                **choices,
            },
        )

    def confirm(self, response):
        self.assertEqual(response.status_code, 200)
        return self.client.post(
            "/library/merge/", {"action": "confirm", "token": response.context["token"]}
        )

    def test_preview_is_read_only_and_zero_score_defaults_preserved(self):
        LibraryLink.objects.create(
            user=self.owner,
            item=self.source.item,
            position="第20章",
            url="https://example.com/book",
        )
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview"]["result"]["score"], Decimal("0"))
        self.assertEqual(response.context["preview"]["result"]["status"], "Completed")
        self.assertEqual(Book.objects.filter(user=self.owner).count(), 2)
        self.assertFalse(
            LibraryLink.objects.filter(user=self.owner, item=self.target.item).exists()
        )
        self.assertEqual(self.confirm(response).url, f"/library/book/{self.target.pk}/")
        self.target.refresh_from_db()
        self.assertEqual(self.target.score, Decimal("0"))
        self.assertEqual(self.target.notes, "相同笔记\n\n来源笔记")
        self.assertEqual(
            LibraryLink.objects.get(user=self.owner, item=self.target.item).position,
            "第20章",
        )
        self.assertTrue(Item.objects.filter(pk=self.source.item_id).exists())

    def test_explicit_empty_values_and_source_score_win(self):
        LibraryLink.objects.create(
            user=self.owner,
            item=self.target.item,
            position="第99章",
            url="https://example.com/keep",
        )
        response = self.confirm(
            self.preview(
                score="source", status="target", position="source", url="source"
            )
        )
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.status, "")
        self.assertEqual(self.target.score, 8)
        self.assertFalse(
            LibraryLink.objects.filter(user=self.owner, item=self.target.item).exists()
        )

    def test_all_history_dates_and_numeric_progress_preserved(self):
        now = timezone.now()
        Book.objects.filter(pk=self.source.pk).update(
            progress=23,
            start_date=now - timedelta(days=20),
            end_date=now - timedelta(days=1),
            progressed_at=now - timedelta(hours=1),
        )
        Book.objects.filter(pk=self.target.pk).update(
            progress=10,
            start_date=now - timedelta(days=2),
            end_date=now - timedelta(days=4),
            progressed_at=now - timedelta(days=2),
        )
        self.source.notes = "追加笔记"
        models.Model.save(self.source, update_fields=["notes"])
        original_ids = set(
            self.source.history.values_list("history_id", flat=True)
        ) | set(self.target.history.values_list("history_id", flat=True))
        self.assertEqual(self.confirm(self.preview()).status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.progress, 23)
        self.assertEqual(self.target.start_date, now - timedelta(days=20))
        self.assertEqual(self.target.end_date, now - timedelta(days=1))
        self.assertEqual(self.target.progressed_at, now - timedelta(hours=1))
        self.assertEqual(self.target.history.count(), len(original_ids) + 1)
        self.assertTrue(
            original_ids.issubset(
                set(self.target.history.values_list("history_id", flat=True))
            )
        )

    def test_owner_scope_kind_and_same_record_are_checked(self):
        private = self.record("private", owner=self.other)
        game = self.record("game", kind="game")
        for source, target in [
            (private, self.target),
            (self.source, private),
            (game, self.target),
            (self.source, self.source),
        ]:
            with self.subTest(source=source.pk, target=target.pk):
                self.assertEqual(self.preview(source, target).status_code, 400)
        response = self.client.get("/library/merge/", {"source": f"book:{private.pk}"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Book.objects.filter(pk=self.source.pk).exists())
        self.assertNotContains(self.client.get("/library/merge/"), "private")

    def test_folders_series_and_other_users_data_preserved(self):
        source_folder = LibraryFolder.objects.create(user=self.owner, name="来源分类")
        source_folder.items.add(self.source.item)
        target_folder = LibraryFolder.objects.create(user=self.owner, name="保留分类")
        target_folder.items.add(self.target.item)
        private_folder = LibraryFolder.objects.create(
            user=self.other, name="其他人的分类"
        )
        private_folder.items.add(self.source.item)
        private_link = LibraryLink.objects.create(
            user=self.other, item=self.source.item, position="私人位置"
        )
        series = LibrarySeries.objects.create(user=self.owner, name="系列")
        LibrarySeriesMember.objects.create(
            series=series, item=self.source.item, label="来源部次", sort_order=2
        )
        LibrarySeriesMember.objects.create(
            series=series, item=self.target.item, label="保留部次", sort_order=9
        )
        extra = LibrarySeries.objects.create(user=self.owner, name="另一关联")
        LibrarySeriesMember.objects.create(
            series=extra, item=self.source.item, label="额外", sort_order=3
        )
        preview = self.preview()
        self.assertContains(preview, "采用保留记录的说明与顺序")
        self.assertEqual(self.confirm(preview).status_code, 302)
        self.assertEqual(
            set(
                LibraryFolder.objects.filter(
                    user=self.owner, items=self.target.item
                ).values_list("pk", flat=True)
            ),
            {source_folder.pk, target_folder.pk},
        )
        self.assertFalse(source_folder.items.filter(pk=self.source.item_id).exists())
        self.assertTrue(private_folder.items.filter(pk=self.source.item_id).exists())
        private_link.refresh_from_db()
        self.assertEqual(private_link.position, "私人位置")
        kept = LibrarySeriesMember.objects.get(series=series, item=self.target.item)
        self.assertEqual((kept.label, kept.sort_order), ("保留部次", 9))
        self.assertTrue(
            LibrarySeriesMember.objects.filter(
                series=extra, item=self.target.item
            ).exists()
        )
        self.assertFalse(
            LibrarySeriesMember.objects.filter(
                series__user=self.owner, item=self.source.item
            ).exists()
        )

    def test_shared_item_other_record_retains_its_links_and_memberships(self):
        another = self.record("another", item=self.source.item)
        link = LibraryLink.objects.create(
            user=self.owner, item=self.source.item, position="第3章"
        )
        folder = LibraryFolder.objects.create(user=self.owner, name="分类")
        folder.items.add(self.source.item)
        group = LibrarySeries.objects.create(user=self.owner, name="系列")
        member = LibrarySeriesMember.objects.create(series=group, item=self.source.item)
        self.assertEqual(self.confirm(self.preview()).status_code, 302)
        self.assertTrue(Book.objects.filter(pk=another.pk).exists())
        self.assertTrue(LibraryLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(folder.items.filter(pk=self.source.item_id).exists())
        self.assertTrue(LibrarySeriesMember.objects.filter(pk=member.pk).exists())

    def test_two_records_of_one_item_keep_item_level_data(self):
        duplicate = self.record("duplicate", item=self.target.item)
        link = LibraryLink.objects.create(
            user=self.owner, item=self.target.item, position="第3章"
        )
        folder = LibraryFolder.objects.create(user=self.owner, name="同一作品")
        folder.items.add(self.target.item)
        self.assertEqual(
            self.confirm(self.preview(duplicate, self.target)).status_code, 302
        )
        self.assertTrue(LibraryLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(folder.items.filter(pk=self.target.item_id).exists())

    def test_tv_without_seasons_merges_but_season_tree_is_rejected(self):
        first, second = self.record("tv-first", "tv"), self.record("tv-second", "tv")
        self.assertEqual(self.confirm(self.preview(first, second)).status_code, 302)
        third = self.record("tv-third", "tv")
        season_item = Item.objects.create(
            source="manual",
            media_type="season",
            media_id="tv-third",
            title="第一季",
            image="",
            season_number=1,
        )
        season = Season(user=self.owner, item=season_item, related_tv=third)
        models.Model.save(season)
        self.assertContains(
            self.preview(third, second), "含分季或分集", status_code=400
        )
        self.assertContains(
            self.preview(second, third), "含分季或分集", status_code=400
        )
        self.assertTrue(Season.objects.filter(pk=season.pk).exists())

    def test_steam_identity_is_kept_and_different_appids_cannot_merge(self):
        steam_item = Item.objects.create(
            source="steam",
            media_type="game",
            media_id="570",
            title="Steam Game",
            image="",
        )
        steam = self.record("steam", "game", item=steam_item, status="In progress")
        local = self.record("local", "game", status="Completed", score=9)
        other_item = Item.objects.create(
            source="steam",
            media_type="game",
            media_id="730",
            title="Other Steam",
            image="",
        )
        other_steam = self.record("other-steam", "game", item=other_item)
        self.assertContains(
            self.preview(steam, local), "Steam 作品选为保留记录", status_code=400
        )
        self.assertContains(
            self.preview(steam, other_steam), "不同 Steam 游戏不能合并", status_code=400
        )
        self.assertEqual(
            self.confirm(self.preview(local, steam, status="source")).status_code, 302
        )
        steam.refresh_from_db()
        self.assertEqual(steam.item.media_id, "570")
        self.assertEqual(steam.status, "Completed")
        self.assertTrue(
            steam.history.filter(history_type="~", status="Completed").exists()
        )
        self.assertEqual(
            Game.objects.filter(user=self.owner, item=steam_item).count(), 1
        )

        SteamConnection.objects.create(
            user=self.owner, steam_id="76561198000000000", encrypted_key="test-only"
        )
        with (
            patch(
                "app.library_tasks.fetch_games",
                return_value=[
                    {"appid": 570, "name": "Steam Game", "playtime_forever": 100}
                ],
            ),
            patch("app.library_tasks.steam.covers", return_value={}),
        ):
            result = sync_steam(self.owner.pk)
        self.assertIn("新增 0 部", result)
        steam.refresh_from_db()
        self.assertEqual(steam.status, "Completed")
        self.assertEqual(steam.score, 9)
        self.assertEqual(steam.progress, 100)
        self.assertEqual(
            Game.objects.filter(user=self.owner, item=steam_item).count(), 1
        )

    def test_changes_after_preview_require_another_preview(self):
        changes = [
            lambda: Book.objects.filter(pk=self.target.pk).update(notes="新笔记"),
            lambda: LibraryLink.objects.create(
                user=self.owner, item=self.source.item, position="新位置"
            ),
            lambda: LibraryFolder.objects.create(
                user=self.owner, name="新分类"
            ).items.add(self.source.item),
            lambda: LibrarySeriesMember.objects.create(
                series=LibrarySeries.objects.create(user=self.owner, name="新系列"),
                item=self.source.item,
            ),
            lambda: models.Model.save(self.source, update_fields=["score"]),
            lambda: self.record("new-related", item=self.source.item),
        ]
        for change in changes:
            with self.subTest(change=change):
                preview = self.preview()
                change()
                self.assertContains(self.confirm(preview), "重新预览", status_code=400)
                self.assertTrue(Book.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.confirm(self.preview()).status_code, 302)

    def test_token_expiry_owner_binding_replay_and_csrf(self):
        preview = self.preview()
        payload = {"action": "confirm", "token": preview.context["token"]}
        self.assertEqual(self.client.get("/library/merge/", payload).status_code, 200)
        self.assertTrue(Book.objects.filter(pk=self.source.pk).exists())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post("/library/merge/", payload).status_code, 403)
        signed = signing.loads(payload["token"], salt=library_merge.TOKEN_SALT)
        with patch("django.core.signing.TimestampSigner.timestamp", return_value="1"):
            expired = signing.dumps(signed, salt=library_merge.TOKEN_SALT)
        session = self.client.session
        pending = session[library_merge.SESSION_KEY]
        session[library_merge.SESSION_KEY] = {**pending, "token": expired}
        session.save()
        self.assertEqual(
            self.client.post(
                "/library/merge/", {"action": "confirm", "token": expired}
            ).status_code,
            400,
        )
        self.client.force_login(self.other)
        session = self.client.session
        session[library_merge.SESSION_KEY] = pending
        session.save()
        self.assertEqual(self.client.post("/library/merge/", payload).status_code, 400)
        self.client.force_login(self.owner)
        current = self.preview()
        self.assertEqual(self.confirm(current).status_code, 302)
        self.assertEqual(self.confirm(current).status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.post("/library/merge/", payload).status_code, 302)

    def test_failure_rolls_back_personal_data_and_history(self):
        preview = self.preview()
        history_ids = list(self.source.history.values_list("history_id", flat=True))
        with patch(
            "app.library_merge.LibraryLink.objects.update_or_create",
            side_effect=DatabaseError("write failure"),
        ):
            # A non-empty link ensures the failure happens after history and record writes.
            LibraryLink.objects.create(
                user=self.owner, item=self.source.item, position="第1章"
            )
            preview = self.preview()
            self.assertContains(self.confirm(preview), "记录尚未更改", status_code=400)
        self.source.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual(self.target.status, "")
        self.assertEqual(
            list(self.source.history.values_list("history_id", flat=True)), history_ids
        )
        self.assertEqual(self.target.history.count(), 1)
        self.assertTrue(Item.objects.filter(pk=self.source.item_id).exists())
