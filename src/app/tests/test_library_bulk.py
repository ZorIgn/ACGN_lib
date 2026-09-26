"""Batch shelf edits validate the complete private selection before writing."""

import json
from decimal import Decimal
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase

from app import library
from app.library_tasks import sync_steam
from app.models import Game, Item, LibraryFolder, SteamConnection
from integrations.imports.helpers import encrypt


class LibraryBulkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="bulk-owner")
        cls.other = get_user_model().objects.create_user(username="bulk-other")

    def setUp(self):
        self.client.force_login(self.owner)

    def record(self, kind="book", *, user=None, **values):
        item = Item.objects.create(
            source="manual",
            media_type=kind,
            media_id=f"bulk-{kind}-{Item.objects.count()}",
            title=f"批量作品{Item.objects.count()}",
        )
        record = library.model_for(kind)(
            user=user or self.owner,
            item=item,
            score=Decimal("8.5"),
            status="Planning",
            notes="保留感想",
            **values,
        )
        models.Model.save(record)
        return record

    def edit(self, records, **data):
        return self.client.post(
            "/library/bulk/",
            {
                "action": "status",
                "status": "Paused",
                "selection": json.dumps(
                    [f"{record.item.media_type}:{record.pk}" for record in records]
                ),
                **data,
            },
        )

    def test_mixed_media_with_the_same_primary_key_are_distinct_selections(self):
        records = [self.record(kind, pk=700) for kind in library.KINDS]
        untouched = self.record()
        response = self.edit(records)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 6)
        for record in records:
            with self.subTest(kind=record.item.media_type):
                record.refresh_from_db()
                self.assertEqual(record.status, "Paused")
                self.assertEqual(
                    (record.score, record.notes), (Decimal("8.5"), "保留感想")
                )
                self.assertEqual(record.progress, 0)
                self.assertIsNone(record.start_date)
                self.assertIsNone(record.end_date)
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, "Planning")

    def test_selection_from_multiple_shelf_pages_updates_only_selected_records(self):
        records = [self.record() for _ in range(40)]
        pages = []
        for page in [1, 2]:
            response = self.client.get("/library/", {"type": "book", "page": page})
            soup = BeautifulSoup(response.content, "html.parser")
            pages.append(
                [node["value"] for node in soup.select("[data-select-record]")]
            )
        self.assertEqual([len(page) for page in pages], [36, 4])
        selection = [pages[0][0], pages[0][3], pages[1][0], pages[1][-1]]
        response = self.edit([], selection=json.dumps(selection), status="Completed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 4)
        for record in records:
            record.refresh_from_db()
            self.assertEqual(
                record.status,
                "Completed" if f"book:{record.pk}" in selection else "Planning",
            )
            self.assertEqual(record.progress, 0)
            self.assertIsNone(record.end_date)

    def test_empty_status_is_explicit_and_duplicate_ids_are_applied_once(self):
        record = self.record(progress=12)
        response = self.edit([record, record], status="", score="1", notes="意外覆盖")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        record.refresh_from_db()
        self.assertEqual(record.status, "")
        self.assertEqual(
            (record.score, record.notes, record.progress),
            (Decimal("8.5"), "保留感想", 12),
        )

    def test_folder_add_and_remove_keep_other_folders_and_unselected_members(self):
        records = [self.record(), self.record("game")]
        untouched = self.record()
        target = LibraryFolder.objects.create(user=self.owner, name="目标分类")
        second = LibraryFolder.objects.create(user=self.owner, name="保留分类")
        private = LibraryFolder.objects.create(user=self.other, name="其他账号分类")
        target.items.add(untouched.item)
        second.items.add(*(record.item for record in records))
        private.items.add(*(record.item for record in records))
        for _ in range(2):
            response = self.edit(records, action="folder_add", folder=target.pk)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["count"], 2)
        self.assertSetEqual(
            set(target.items.values_list("pk", flat=True)),
            {record.item_id for record in [*records, untouched]},
        )
        response = self.edit(records, action="folder_remove", folder=target.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(target.items.values_list("pk", flat=True)), [untouched.item_id]
        )
        for folder in [second, private]:
            self.assertSetEqual(
                set(folder.items.values_list("pk", flat=True)),
                {record.item_id for record in records},
            )
        for record in records:
            record.refresh_from_db()
            self.assertEqual(
                (record.status, record.score, record.notes),
                ("Planning", Decimal("8.5"), "保留感想"),
            )

    def test_late_missing_or_foreign_record_prevents_every_status_or_folder_write(self):
        own = self.record()
        foreign = self.record("game", user=self.other)
        folder = LibraryFolder.objects.create(user=self.owner, name="测试分类")
        for invalid in ["game:999999999", f"game:{foreign.pk}"]:
            for action in ["status", "folder_add", "folder_remove"]:
                with self.subTest(invalid=invalid, action=action):
                    folder.items.set([own.item])
                    count = own.history.count()
                    response = self.edit(
                        [],
                        action=action,
                        folder=folder.pk,
                        selection=json.dumps([f"book:{own.pk}", invalid]),
                    )
                    self.assertEqual(response.status_code, 400)
                    own.refresh_from_db()
                    foreign.refresh_from_db()
                    self.assertEqual(own.status, "Planning")
                    self.assertEqual(foreign.status, "Planning")
                    self.assertEqual(own.history.count(), count)
                    self.assertEqual(
                        list(folder.items.values_list("pk", flat=True)), [own.item_id]
                    )

    def test_foreign_and_missing_folders_cannot_change_membership(self):
        record = self.record()
        private = LibraryFolder.objects.create(user=self.other, name="私人分类")
        private.items.add(record.item)
        for folder in [private.pk, 999999999]:
            for action in ["folder_add", "folder_remove"]:
                with self.subTest(folder=folder, action=action):
                    response = self.edit([record], action=action, folder=folder)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        list(private.items.values_list("pk", flat=True)),
                        [record.item_id],
                    )
                    record.refresh_from_db()
                    self.assertEqual(record.status, "Planning")

    def test_invalid_selection_action_status_and_folder_are_rejected_without_writes(
        self,
    ):
        record = self.record()
        invalid_payloads = [
            {"selection": "not-json"},
            {"selection": "{}"},
            {"selection": "[]"},
            {"selection": json.dumps([f"book:{record.pk}", 1])},
            {"selection": json.dumps([f"book:{record.pk}", "book:0"])},
            {"selection": json.dumps([f"book:{record.pk}", "season:1"])},
            {
                "selection": json.dumps(
                    [f"book:{record.pk}", "book:9223372036854775808"]
                )
            },
            {"action": "delete"},
            {"status": "invalid"},
            {"action": "folder_add", "folder": ""},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.edit([record], **payload)
                self.assertEqual(response.status_code, 400)
                record.refresh_from_db()
                self.assertEqual(record.status, "Planning")
                self.assertEqual(record.history.count(), 1)

    def test_bulk_changes_require_post_and_login(self):
        record = self.record()
        self.assertEqual(self.client.get("/library/bulk/").status_code, 405)
        self.client.logout()
        response = self.edit([record])
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        record.refresh_from_db()
        self.assertEqual(record.status, "Planning")

    def test_explicit_steam_planning_status_survives_later_playtime_sync(self):
        SteamConnection.objects.create(
            user=self.owner,
            steam_id="76561198000000000",
            encrypted_key=encrypt("a" * 32),
        )
        games = [{"appid": 123, "name": "手动状态游戏", "playtime_forever": 0}]
        with (
            patch("app.library_tasks.fetch_games", return_value=games),
            patch("app.library_tasks.steam.covers", return_value={}),
        ):
            sync_steam(self.owner.pk)
            record = Game.objects.get(user=self.owner)
            self.assertIsNone(record.score)
            self.assertEqual(record.notes, "")
            response = self.edit([record], status="Planning")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                record.history.filter(history_type="~", status="Planning").exists()
            )
            games[0]["playtime_forever"] = 120
            sync_steam(self.owner.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, "Planning")
        self.assertEqual(record.progress, 120)
        self.assertIsNone(record.start_date)
        self.assertIsNone(record.end_date)
