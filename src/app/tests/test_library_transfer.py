"""Portable library data stays private and imports only after confirmation."""

import csv
import io
import json
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, models
from django.test import Client, TestCase
from django.urls import reverse

from app import library_transfer as transfer
from app.library import KINDS, model_for
from app.library_tasks import sync_steam
from app.models import Item, LibraryFolder, LibraryLink, SteamConnection


class LibraryTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="transfer-owner")
        cls.other = get_user_model().objects.create_user(username="transfer-other")

    def setUp(self):
        self.client.force_login(self.owner)
        self.url = reverse("library_transfer")

    def work(self, kind="book", media_id="transfer-book", **values):
        return {
            "source": "manual",
            "media_type": kind,
            "media_id": media_id,
            "title": f"作品 {kind}",
            "image": "/static/img/none.svg",
            "score": "0.0",
            "status": "",
            "notes": '我的记录，"保留原文"\n下一行',
            "viewing_url": "https://example.org/read/1",
            "position": "第 12 章",
            "folders": ["最爱"],
            "progress_minutes": 123 if kind == "game" else None,
            **values,
        }

    def payload(self, works=None):
        return {
            "format": transfer.FORMAT,
            "version": transfer.VERSION,
            "folders": ["最爱", "空文件夹"],
            "series": [],
            "works": works if works is not None else [self.work()],
        }

    def upload(self, payload=None, raw=None):
        if raw is None:
            raw = json.dumps(payload or self.payload(), ensure_ascii=False).encode()
        return self.client.post(
            self.url,
            {
                "action": "preview",
                "file": SimpleUploadedFile("library.json", raw, "application/json"),
            },
        )

    def confirm(self, **values):
        pending = self.client.session.get(transfer.SESSION_KEY, {})
        return self.client.post(
            self.url,
            {"action": "confirm", "token": pending.get("token", ""), **values},
        )

    def assert_library_empty(self, owner=None):
        owner = owner or self.owner
        for kind in KINDS:
            self.assertFalse(model_for(kind).objects.filter(user=owner).exists())
        self.assertFalse(LibraryLink.objects.filter(user=owner).exists())
        self.assertFalse(LibraryFolder.objects.filter(user=owner).exists())

    def create_record(self, user, work):
        item, _ = Item.objects.get_or_create(
            source=work["source"],
            media_type=work["media_type"],
            media_id=work["media_id"],
            defaults={"title": work["title"], "image": work["image"]},
        )
        record = model_for(work["media_type"])(
            user=user,
            item=item,
            score=work["score"],
            status=work["status"],
            notes=work["notes"],
        )
        if work["media_type"] == "game":
            record.progress = work["progress_minutes"]
        models.Model.save(record)
        LibraryLink.objects.create(
            user=user, item=item, url=work["viewing_url"], position=work["position"]
        )
        for name in work["folders"]:
            folder, _ = LibraryFolder.objects.get_or_create(user=user, name=name)
            folder.items.add(item)
        return record

    @patch("requests.sessions.Session.request", side_effect=AssertionError("network"))
    @patch(
        "app.providers.services.get_media_metadata",
        side_effect=AssertionError("provider"),
    )
    def test_all_six_types_roundtrip_preserves_personal_data_and_empty_folders(
        self, metadata, network
    ):
        works = [
            self.work(kind, kind, score=None if kind == "manga" else "0.0")
            for kind in KINDS
        ]
        for work in works:
            self.create_record(self.owner, work)
        LibraryFolder.objects.create(user=self.owner, name="空文件夹")
        self.create_record(self.other, self.work(media_id="private", notes="他人秘密"))
        LibraryFolder.objects.create(user=self.other, name="他人文件夹")
        SteamConnection.objects.create(
            user=self.owner,
            steam_id="12345678901234567",
            encrypted_key="private-api-key",
        )
        response = self.client.get(self.url, {"format": "json"})
        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertEqual(set(exported), {"format", "version", "folders", "works", "series"})
        self.assertEqual(len(exported["works"]), 6)
        self.assertEqual({work["media_type"] for work in exported["works"]}, set(KINDS))
        self.assertEqual(exported["folders"], ["最爱", "空文件夹"])
        self.assertNotContains(response, "他人秘密")
        self.assertNotContains(response, "他人文件夹")
        self.assertNotContains(response, "private-api-key")
        self.assertNotContains(response, "12345678901234567")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])

        recipient = get_user_model().objects.create_user(username="transfer-recipient")
        self.client.force_login(recipient)
        self.assertEqual(self.upload(exported).status_code, 200)
        self.assert_library_empty(recipient)
        self.assertEqual(self.confirm().status_code, 302)
        self.assertEqual(transfer.export_payload(recipient), exported)
        self.assertEqual(model_for("game").objects.get(user=recipient).progress, 123)
        self.assertIsNone(model_for("manga").objects.get(user=recipient).score)
        self.assertEqual(
            model_for("book").objects.get(user=recipient).score, Decimal("0")
        )
        self.assertFalse(
            LibraryFolder.objects.get(user=recipient, name="空文件夹").items.exists()
        )
        metadata.assert_not_called()
        network.assert_not_called()

    @patch("requests.sessions.Session.request", side_effect=AssertionError("network"))
    @patch(
        "app.providers.services.get_media_metadata",
        side_effect=AssertionError("provider"),
    )
    def test_preview_does_not_write_and_confirm_uses_stored_payload(
        self, metadata, network
    ):
        works = [self.work(kind, kind) for kind in KINDS]
        response = self.upload(self.payload(works))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview"]["new"], 6)
        self.assertEqual(response.context["preview"]["new_folders"], 2)
        self.assert_library_empty()
        self.assertFalse(Item.objects.exists())
        self.assertEqual(
            self.client.get(self.url).context["preview"], response.context["preview"]
        )
        self.assertEqual(self.confirm(payload="untrusted replacement").status_code, 302)
        self.assertNotIn(transfer.SESSION_KEY, self.client.session)
        self.assertEqual(Item.objects.count(), 6)
        for kind in KINDS:
            record = model_for(kind).objects.get(user=self.owner)
            self.assertEqual(record.status, "")
            self.assertEqual(record.score, Decimal("0"))
        metadata.assert_not_called()
        network.assert_not_called()

    def test_idempotent_import_keeps_existing_records_links_folders_and_shared_metadata(
        self,
    ):
        existing = self.create_record(
            self.owner, self.work(score="8.0", status="Paused")
        )
        shared = self.create_record(
            self.other,
            self.work("game", "shared-game", title="共享标题", progress_minutes=77),
        )
        before = transfer.export_payload(self.owner)
        other_before = transfer.export_payload(self.other)
        imported = self.payload(
            [
                self.work(score="1.0", notes="替换记录", folders=["空文件夹"]),
                self.work("game", "shared-game", title="不同标题", progress_minutes=14),
            ]
        )
        response = self.upload(imported)
        self.assertEqual(response.context["preview"]["existing"], 1)
        self.assertEqual(response.context["preview"]["new"], 1)
        self.assertEqual(self.confirm().status_code, 302)
        existing.refresh_from_db()
        self.assertEqual(existing.score, Decimal("8.0"))
        self.assertEqual(existing.status, "Paused")
        self.assertEqual(
            transfer.export_payload(self.owner)["works"][0], before["works"][0]
        )
        self.assertFalse(
            LibraryFolder.objects.get(user=self.owner, name="空文件夹").items.exists()
        )
        self.assertEqual(transfer.export_payload(self.other), other_before)
        shared.item.refresh_from_db()
        self.assertEqual(shared.item.title, "共享标题")
        self.assertEqual(model_for("game").objects.get(user=self.owner).progress, 14)
        after = transfer.export_payload(self.owner)
        self.assertEqual(self.upload(imported).context["preview"]["new"], 0)
        self.assertEqual(self.confirm().status_code, 302)
        self.assertEqual(transfer.export_payload(self.owner), after)
        self.assertEqual(self.confirm().status_code, 400)

    def test_duplicate_works_import_first_entry_and_position_without_url(self):
        first = self.work(viewing_url="", position="第 3 集")
        second = {**first, "notes": "第二条记录", "position": "第 4 集"}
        response = self.upload(self.payload([first, second]))
        self.assertEqual(response.context["preview"]["total"], 2)
        self.assertEqual(response.context["preview"]["new"], 1)
        self.assertEqual(response.context["preview"]["duplicates"], 1)
        self.assertEqual(self.confirm().status_code, 302)
        self.assertEqual(model_for("book").objects.get().notes, first["notes"])
        self.assertEqual(LibraryLink.objects.get().url, "")
        self.assertEqual(LibraryLink.objects.get().position, "第 3 集")

    @patch("app.library_tasks.steam.covers", return_value={})
    @patch(
        "app.library_tasks.fetch_games",
        return_value=[{"appid": 123, "name": "Steam game", "playtime_forever": 120}],
    )
    def test_steam_roundtrip_preserves_empty_link_in_detail_and_after_sync(
        self, fetch_games, covers
    ):
        self.create_record(
            self.owner,
            self.work("game", "123", source="steam", viewing_url="", position=""),
        )
        exported = transfer.export_payload(self.owner)
        self.client.force_login(self.other)
        self.assertEqual(self.upload(exported).status_code, 200)
        self.assertEqual(self.confirm().status_code, 302)
        record = model_for("game").objects.get(user=self.other)
        path = f"/library/game/{record.pk}/"
        self.assertEqual(
            self.client.get(path).context["form"]["viewing_url"].value(), ""
        )
        SteamConnection.objects.create(
            user=self.other, steam_id="76561198000000000", encrypted_key="test-key"
        )
        sync_steam(self.other.pk)
        link = LibraryLink.objects.get(user=self.other, item=record.item)
        self.assertEqual(link.url, "")
        self.assertEqual(link.position, "")
        self.assertEqual(
            self.client.get(path).context["form"]["viewing_url"].value(), ""
        )
        fetch_games.assert_called_once()
        covers.assert_called_once()

    @patch("app.library_tasks.steam.covers", return_value={})
    @patch(
        "app.library_tasks.fetch_games",
        return_value=[{"appid": 123, "name": "Steam game", "playtime_forever": 120}],
    )
    def test_steam_roundtrip_preserves_explicit_planning_when_sync_updates_minutes(
        self, fetch_games, covers
    ):
        original = self.create_record(
            self.owner,
            self.work(
                "game",
                "123",
                source="steam",
                score=None,
                notes="",
                status="Planning",
                progress_minutes=0,
            ),
        )
        models.Model.save(original, update_fields=["status"])
        exported = transfer.export_payload(self.owner)
        self.client.force_login(self.other)
        self.assertEqual(self.upload(exported).status_code, 200)
        self.assertEqual(self.confirm().status_code, 302)
        SteamConnection.objects.create(
            user=self.other, steam_id="76561198000000000", encrypted_key="test-key"
        )
        sync_steam(self.other.pk)
        record = model_for("game").objects.get(user=self.other)
        self.assertEqual(record.status, "Planning")
        self.assertEqual(record.progress, 120)
        self.assertIsNone(record.score)
        self.assertEqual(record.notes, "")
        self.assertIsNone(record.start_date)
        self.assertIsNone(record.end_date)
        original.refresh_from_db()
        self.assertEqual(original.status, "Planning")
        self.assertEqual(original.progress, 0)
        fetch_games.assert_called_once()
        covers.assert_called_once()

    def test_invalid_json_and_shapes_reject_entire_file_and_clear_previous_preview(
        self,
    ):
        malformed = [
            b"{",
            b"\xff",
            b'{"format":"acglib-library","format":"acglib-library"}',
            b'{"score":NaN}',
            b"[" * 2000 + b"]" * 2000,
            b"null",
            b"[]",
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:80]):
                self.assertEqual(self.upload().status_code, 200)
                self.assertEqual(self.upload(raw=raw).status_code, 400)
                self.assertNotIn(transfer.SESSION_KEY, self.client.session)
                self.assert_library_empty()
        invalid = [
            {**self.payload(), "version": True},
            {**self.payload(), "version": 99},
            {**self.payload(), "format": "other"},
            {**self.payload(), "credentials": "secret"},
            {**self.payload(), "folders": {}},
            {**self.payload(), "folders": ["最爱", "最爱"]},
            {**self.payload(), "works": {}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.upload(payload).status_code, 400)
                self.assert_library_empty()
        self.assertFalse(Item.objects.exists())

    def test_invalid_second_work_prevents_every_write(self):
        invalid_values = [
            {"source": []},
            {"source": "unknown"},
            {"media_type": {}},
            {"media_type": "episode"},
            {"status": []},
            {"status": "unknown"},
            {"media_id": " "},
            {"title": ""},
            {"score": True},
            {"score": "NaN"},
            {"score": "Infinity"},
            {"score": "10.1"},
            {"score": "-1"},
            {"score": "0.01"},
            {"score": "9.00000000000000000000000000001"},
            {"score": {}},
            {"notes": "\ud800"},
            {"notes": "\x00"},
            {"position": "x" * 121},
            {"viewing_url": "javascript:alert(1)"},
            {"image": "//example.org/image.png"},
            {"viewing_url": "https://example.org/\n"},
            {"folders": ["未声明"]},
            {"folders": ["最爱", "最爱"]},
            {"progress_minutes": 5},
            {"unexpected": "value"},
        ]
        for values in invalid_values:
            with self.subTest(values=values):
                payload = self.payload(
                    [self.work(), self.work(**{"media_id": "second", **values})]
                )
                raw = json.dumps(payload).encode()
                self.assertEqual(self.upload(raw=raw).status_code, 400)
                self.assert_library_empty()
                self.assertFalse(Item.objects.exists())
        for progress in [-1, True, 2.5, None, 2147483648]:
            with self.subTest(progress=progress):
                payload = self.payload([self.work("game", progress_minutes=progress)])
                self.assertEqual(self.upload(payload).status_code, 400)
        for entry in [None, [], {"title": "不完整"}]:
            self.assertEqual(self.upload(self.payload([entry])).status_code, 400)

    def test_json_numeric_scores_preserve_precision_during_validation(self):
        for value in ["9.00000000000000000000000000001", "1e-1000001"]:
            raw = json.dumps(self.payload()).replace(
                '"score": "0.0"', f'"score": {value}'
            )
            self.assertEqual(self.upload(raw=raw.encode()).status_code, 400)
            self.assert_library_empty()
        raw = json.dumps(self.payload()).replace('"score": "0.0"', '"score": 9.5')
        self.assertEqual(self.upload(raw=raw.encode()).status_code, 200)
        self.assertEqual(self.confirm().status_code, 302)
        self.assertEqual(model_for("book").objects.get().score, Decimal("9.5"))

    def test_database_failure_rolls_back_items_records_links_and_folders(self):
        payload = transfer.validate_payload(
            self.payload([self.work(), self.work("game", "second")])
        )
        create_item = Item.objects.get_or_create

        def fail_second_item(**kwargs):
            if kwargs["media_id"] == "second":
                raise IntegrityError("Import interrupted")
            return create_item(**kwargs)

        with patch.object(Item.objects, "get_or_create", side_effect=fail_second_item):
            with self.assertRaises(IntegrityError):
                transfer.import_payload(self.owner, payload)
        self.assert_library_empty()
        self.assertFalse(Item.objects.exists())
        self.assertFalse(model_for("book").history.exists())

    def test_confirm_revalidates_stored_payload(self):
        self.upload()
        session = self.client.session
        session[transfer.SESSION_KEY]["payload"]["works"].append(self.work(score="99"))
        session.save()
        self.assertEqual(self.confirm().status_code, 400)
        self.assert_library_empty()

    def test_confirmation_is_bound_to_token_session_and_owner(self):
        self.assertEqual(self.confirm().status_code, 400)
        self.upload()
        for token in ["", "incorrect", "错误令牌"]:
            self.assertEqual(self.confirm(token=token).status_code, 400)
            self.assert_library_empty()
        pending = deepcopy(self.client.session[transfer.SESSION_KEY])
        second_client = Client()
        second_client.force_login(self.owner)
        self.assertEqual(
            second_client.post(
                self.url, {"action": "confirm", "token": pending["token"]}
            ).status_code,
            400,
        )
        self.client.force_login(self.other)
        session = self.client.session
        session[transfer.SESSION_KEY] = pending
        session.save()
        self.assertNotIn("preview", self.client.get(self.url).context)
        self.assertEqual(self.confirm().status_code, 400)
        self.assert_library_empty(self.other)
        self.assert_library_empty(self.owner)

    def test_authentication_and_csrf_are_required(self):
        anonymous = Client()
        for file_format in [None, "json", "csv"]:
            response = anonymous.get(
                self.url, {"format": file_format} if file_format else {}
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("login", response.url)
        self.assertEqual(
            anonymous.post(self.url, {"action": "confirm"}).status_code, 302
        )
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.owner)
        self.assertEqual(
            protected.post(self.url, {"action": "preview"}).status_code, 403
        )
        self.assertEqual(
            protected.post(self.url, {"action": "confirm"}).status_code, 403
        )
        self.assert_library_empty()

    def test_file_limit_checks_reported_size_and_actual_content(self):
        self.assertEqual(
            self.upload(raw=b" " * (transfer.MAX_FILE_BYTES + 1)).status_code, 400
        )
        content = io.BytesIO(b" " * (transfer.MAX_FILE_BYTES + 1))
        content.size = 1
        with self.assertRaisesMessage(ValidationError, "5 MiB"):
            transfer._read_upload(content)
        self.assertEqual(
            self.client.post(self.url, {"action": "preview"}).status_code, 400
        )
        self.assert_library_empty()

    def test_utf8_bom_json_is_accepted(self):
        raw = b"\xef\xbb\xbf" + json.dumps(self.payload()).encode()
        self.assertEqual(self.upload(raw=raw).status_code, 200)
        self.assertEqual(self.confirm().status_code, 302)

    def test_csv_has_bom_quotes_and_formula_escaping_without_changing_json(self):
        work = self.work(
            title='=SUM(1,2),"标题"',
            notes='  +cmd, "引号"\n第二行',
            position="\t=1+1",
            folders=["@最爱"],
        )
        self.create_record(self.owner, work)
        self.create_record(self.other, self.work(media_id="private", title="他人作品"))
        response = self.client.get(self.url, {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn('filename="acglib-library.csv"', response["Content-Disposition"])
        content = response.content.decode("utf-8-sig")
        self.assertTrue(content.startswith('"来源","类型",'))
        rows = list(csv.DictReader(io.StringIO(content, newline="")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["标题"], "'" + work["title"])
        self.assertEqual(rows[0]["个人记录"], "'" + work["notes"])
        self.assertEqual(rows[0]["观看 / 阅读位置"], "'" + work["position"])
        self.assertEqual(rows[0]["收藏文件夹"], "'@最爱")
        self.assertEqual(rows[0]["评分"], "0.0")
        self.assertEqual(rows[0]["状态"], "未设置")
        exported = self.client.get(self.url, {"format": "json"}).json()
        self.assertEqual(exported["works"][0]["title"], work["title"])
        self.assertEqual(exported["works"][0]["notes"], work["notes"])

    def test_invalid_operations_are_rejected_without_writes(self):
        self.assertEqual(self.client.get(self.url, {"format": "xml"}).status_code, 400)
        self.assertEqual(
            self.client.post(self.url, {"action": "replace"}).status_code, 400
        )
        self.assertEqual(self.client.put(self.url).status_code, 405)
        self.assert_library_empty()
