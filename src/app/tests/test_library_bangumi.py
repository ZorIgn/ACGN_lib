"""Public Bangumi imports remain reviewed, private and repeatable."""

import time
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.test import Client, TestCase, override_settings
from django.urls import path, reverse

from app import library_bangumi as importer
from app.library import KINDS, model_for
from app.models import Item, LibraryFolder, LibraryLink
from app.providers import services
from config.native_urls import urlpatterns as native_patterns

urlpatterns = [
    path("library/bangumi/", importer.bangumi_import, name="library_bangumi"),
    *native_patterns,
]


def collection(subject_id=11, subject_type=2, **values):
    return {
        "subject_id": subject_id,
        "subject_type": subject_type,
        "rate": 0,
        "type": 3,
        "comment": "原文：很喜欢\n准备重看",
        "tags": [],
        "ep_status": 5,
        "vol_status": 0,
        "private": False,
        "subject": {
            "id": subject_id,
            "type": subject_type,
            "name": f"Original {subject_id}",
            "name_cn": f"作品 {subject_id}",
            "images": {"large": f"https://lain.bgm.tv/pic/cover/l/{subject_id}.jpg"},
        },
        **values,
    }


def page(entries, offset=0, total=None):
    return {
        "data": entries,
        "offset": offset,
        "limit": importer.PAGE_SIZE,
        "total": len(entries) if total is None else total,
    }


def provider_error(status=None):
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(response=response) if status else requests.Timeout()
    return services.ProviderAPIError("bangumi", error)


@override_settings(ROOT_URLCONF=__name__)
class BangumiImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="bangumi-owner")
        cls.other = get_user_model().objects.create_user(username="bangumi-other")

    def setUp(self):
        self.client.force_login(self.owner)
        self.url = reverse("library_bangumi")

    def assert_empty(self):
        self.assertFalse(Item.objects.exists())
        self.assertFalse(LibraryLink.objects.exists())
        self.assertFalse(LibraryFolder.objects.exists())
        for kind in KINDS:
            self.assertFalse(model_for(kind).objects.exists())

    def preview(self, entries=None):
        if entries is None:
            entries = [collection()]
        with patch.object(importer.bangumi, "request", return_value=page(entries)):
            return self.client.post(self.url, {"action": "preview", "username": "sai"})

    def confirm(self, token=None):
        if token is None:
            token = self.client.session.get(importer.SESSION_KEY, {}).get("token", "")
        return self.client.post(self.url, {"action": "confirm", "token": token})

    def replace_pending(self, **values):
        session = self.client.session
        session[importer.SESSION_KEY] = {**session[importer.SESSION_KEY], **values}
        session.save()

    def test_pagination_reads_every_type_and_skips_music(self):
        first = [collection(index) for index in range(1, 51)]
        second = [collection(51, 4), collection(52, 3)]
        with patch.object(
            importer.bangumi,
            "request",
            side_effect=[page(first, total=52), page(second, offset=50, total=52)],
        ) as request:
            payload, skipped = importer.fetch_collection("public_user")
        self.assertEqual(len(payload["works"]), 51)
        self.assertEqual(skipped, 1)
        self.assertEqual(payload["works"][-1]["media_type"], "game")
        self.assertEqual(payload["works"][-1]["progress_minutes"], 0)
        self.assertEqual(
            [call.kwargs["params"] for call in request.call_args_list],
            [{"limit": 50, "offset": 0}, {"limit": 50, "offset": 50}],
        )
        self.assertTrue(
            all(
                call.args[0] == "users/public_user/collections"
                for call in request.call_args_list
            )
        )
        self.assert_empty()

    def test_mapping_preserves_status_rating_notes_and_progress(self):
        entries = [
            collection(1, 1, type=1, rate=0, ep_status=12, vol_status=2),
            collection(2, 1, type=2, rate=9, ep_status=80, vol_status=0),
            collection(3, 2, type=3, rate=5),
            collection(4, 6, type=4, rate=1),
            collection(5, 4, type=5, rate=10),
        ]
        details = {
            "1": {**entries[0]["subject"], "platform": "漫画"},
            "2": {**entries[1]["subject"], "platform": "小说"},
        }
        with patch.object(importer.bangumi, "subject", side_effect=details.__getitem__):
            response = self.preview(entries)
        self.assertEqual(response.status_code, 200)
        self.assert_empty()
        works = self.client.session[importer.SESSION_KEY]["payload"]["works"]
        self.assertEqual(
            [w["media_type"] for w in works], ["manga", "book", "anime", "tv", "game"]
        )
        self.assertEqual(
            [w["status"] for w in works], list(importer.STATUS_MAP.values())
        )
        self.assertEqual(
            [w["score"] for w in works], [None, "9.0", "5.0", "1.0", "10.0"]
        )
        self.assertEqual(works[0]["position"], "已读 2 卷 · 已读 12 话")
        self.assertEqual(works[1]["position"], "已读 80 章")
        self.assertEqual(works[3]["position"], "已看 5 集")
        self.assertEqual(works[4]["position"], "")
        self.assertEqual(works[0]["notes"], entries[0]["comment"])
        self.assertEqual(self.confirm().status_code, 302)
        for work in works:
            record = model_for(work["media_type"]).objects.get(user=self.owner)
            self.assertEqual(record.status, work["status"])
            self.assertEqual(record.notes, work["notes"])
            expected = Decimal(work["score"]) if work["score"] is not None else None
            self.assertEqual(record.score, expected)
            if work["position"]:
                link = LibraryLink.objects.get(user=self.owner, item=record.item)
                self.assertEqual(link.position, work["position"])
                self.assertEqual(link.url, "")

    def test_preview_requires_login_post_and_csrf_and_get_does_not_fetch(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(
            self.client.post(self.url, {"action": "preview"}).status_code, 302
        )
        self.client.force_login(self.owner)
        with patch.object(importer.bangumi, "request") as request:
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            request.assert_not_called()
        self.assertEqual(self.client.put(self.url).status_code, 405)
        self.assertNotIn(importer.SESSION_KEY, self.client.session)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(
            csrf.post(self.url, {"action": "preview", "username": "sai"}).status_code,
            403,
        )
        self.assert_empty()

    def test_invalid_username_and_action_do_not_request_provider(self):
        with patch.object(importer.bangumi, "request") as request:
            for username in [
                "",
                "https://bgm.tv/user/sai",
                "../me",
                "a?token=secret",
                "a" * 65,
            ]:
                with self.subTest(username=username):
                    response = self.client.post(
                        self.url, {"action": "preview", "username": username}
                    )
                    self.assertEqual(response.status_code, 400)
            self.assertEqual(
                self.client.post(self.url, {"action": "overwrite"}).status_code, 400
            )
            request.assert_not_called()
        self.assert_empty()

    def test_pending_belongs_to_owner_expires_and_confirm_consumes_token(self):
        self.preview()
        first = deepcopy(self.client.session[importer.SESSION_KEY])
        self.assertEqual(self.confirm("wrong").status_code, 400)
        self.assertEqual(self.confirm("任意文字").status_code, 400)
        self.replace_pending(owner=str(self.other.pk))
        self.assertEqual(self.confirm(first["token"]).status_code, 400)
        self.replace_pending(
            owner=str(self.owner.pk), created=time.time() - importer.PREVIEW_SECONDS - 1
        )
        self.assertEqual(self.confirm(first["token"]).status_code, 400)
        self.assert_empty()
        self.replace_pending(created=time.time())
        self.assertEqual(self.client.get(self.url).status_code, 200)
        with patch.object(importer.bangumi, "request") as request:
            self.assertEqual(self.confirm(first["token"]).status_code, 302)
            self.assertEqual(self.confirm(first["token"]).status_code, 400)
            request.assert_not_called()
        self.assertEqual(model_for("anime").objects.count(), 1)

    def test_existing_records_and_shared_metadata_are_preserved(self):
        item = Item.objects.create(
            source="bangumi",
            media_type="anime",
            media_id="11",
            title="本机标题",
            image="/static/img/none.svg",
        )
        record = model_for("anime")(
            user=self.owner, item=item, score=0, status="", notes="手写原文"
        )
        models.Model.save(record)
        link = LibraryLink.objects.create(
            user=self.owner,
            item=item,
            url="https://example.org/read",
            position="自定位置",
        )
        folder = LibraryFolder.objects.create(user=self.owner, name="我的分类")
        folder.items.add(item)
        other_record = model_for("anime")(
            user=self.other, item=item, score=7, status="Paused", notes="其他用户"
        )
        models.Model.save(other_record)
        response = self.preview(
            [collection(), collection(12, rate=6), collection(12, rate=10)]
        )
        self.assertEqual(response.context["preview"]["existing"], 1)
        self.assertEqual(response.context["preview"]["duplicates"], 1)
        self.assertEqual(response.context["preview"]["new"], 1)
        self.assertEqual(self.confirm().status_code, 302)
        record.refresh_from_db()
        other_record.refresh_from_db()
        item.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(
            (record.score, record.status, record.notes), (0, "", "手写原文")
        )
        self.assertEqual((other_record.score, other_record.notes), (7, "其他用户"))
        self.assertEqual(item.title, "本机标题")
        self.assertEqual(
            (link.url, link.position), ("https://example.org/read", "自定位置")
        )
        self.assertEqual(list(folder.items.all()), [item])
        self.assertEqual(
            model_for("anime").objects.get(user=self.owner, item__media_id="12").score,
            6,
        )
        self.preview([collection(), collection(12)])
        self.assertEqual(self.confirm().status_code, 302)
        self.assertEqual(model_for("anime").objects.filter(user=self.owner).count(), 2)

    def test_partial_fetch_failure_discards_preview_and_leaves_library_untouched(self):
        self.preview()
        first_page = page([collection(index) for index in range(1, 51)], total=51)
        with patch.object(
            importer.bangumi, "request", side_effect=[first_page, provider_error()]
        ):
            response = self.client.post(
                self.url, {"action": "preview", "username": "sai"}
            )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(importer.SESSION_KEY, self.client.session)
        self.assertContains(response, "暂时无法获取完整", status_code=502)
        self.assert_empty()

    def test_collection_changed_between_pages_discards_entire_preview(self):
        first_page = page([collection(index) for index in range(1, 51)], total=51)
        for final_page in [
            page([], offset=50, total=50),
            page([collection(51)], offset=50, total=52),
            page([collection(51), collection(52)], offset=50, total=51),
        ]:
            with self.subTest(final_page=final_page):
                self.preview()
                with patch.object(
                    importer.bangumi, "request", side_effect=[first_page, final_page]
                ):
                    response = self.client.post(
                        self.url, {"action": "preview", "username": "sai"}
                    )
                self.assertContains(response, "分页已变化", status_code=400)
                self.assertNotIn(importer.SESSION_KEY, self.client.session)
                self.assert_empty()

    def test_provider_errors_show_retry_or_username_advice(self):
        for code, expected_status, expected_text in [
            (404, 400, "未找到该"),
            (429, 502, "限制请求"),
            (500, 502, "检查网络"),
        ]:
            with (
                self.subTest(code=code),
                patch.object(
                    importer.bangumi, "request", side_effect=provider_error(code)
                ),
            ):
                response = self.client.post(
                    self.url, {"action": "preview", "username": "sai"}
                )
                self.assertContains(
                    response, expected_text, status_code=expected_status
                )
        self.assert_empty()

    def test_unavailable_subjects_and_private_entries_are_explicitly_skipped(self):
        entries = [
            collection(),
            collection(12, subject=None),
            collection(13, 1),
            collection(14, private=True),
            collection(15, 3),
        ]
        with patch.object(importer.bangumi, "subject", side_effect=provider_error(404)):
            response = self.preview(entries)
        self.assertEqual(response.context["skipped"], 4)
        self.assertEqual(response.context["preview"]["new"], 1)
        self.assert_empty()

    def test_invalid_provider_data_never_reaches_confirmation(self):
        malformed = [
            page([collection(rate=-1)]),
            page([collection(rate=True)]),
            page([collection(type=0)]),
            page([collection(ep_status=-1)]),
            page([collection(subject_type="2")]),
            page([collection(private=1)]),
            page([collection(subject={"id": 100, "type": 2})]),
            page(
                [
                    collection(
                        subject={
                            **collection()["subject"],
                            "images": {"large": "javascript:alert(1)"},
                        }
                    )
                ]
            ),
            page([], total=1),
            page([collection()], total=0),
            page([], offset=1),
            page([], total=importer.MAX_COLLECTIONS + 1),
        ]
        for result in malformed:
            with (
                self.subTest(result=result),
                patch.object(importer.bangumi, "request", return_value=result),
            ):
                response = self.client.post(
                    self.url, {"action": "preview", "username": "sai"}
                )
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(importer.SESSION_KEY, self.client.session)
        self.assert_empty()

    def test_payload_size_is_bounded_and_provider_text_is_escaped(self):
        with (
            patch.object(importer.library_transfer, "MAX_FILE_BYTES", 20),
            patch.object(
                importer.bangumi, "request", return_value=page([collection()])
            ),
        ):
            with self.assertRaises(ValidationError):
                importer.fetch_collection("sai")
        response = self.preview([collection(comment="<script>alert(1)</script>")])
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assert_empty()
