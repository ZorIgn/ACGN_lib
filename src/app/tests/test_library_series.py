"""Owner-bound series retain independent records and survive data migration."""

from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase

from app import library_transfer as transfer
from app.models import Book, Item, LibraryFolder, LibrarySeries, LibrarySeriesMember


class SeriesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="series-owner")
        cls.other = get_user_model().objects.create_user(username="series-other")
        cls.group = LibrarySeries.objects.create(
            user=cls.owner, name="星海", kind="volume"
        )
        cls.private = LibrarySeries.objects.create(user=cls.other, name="私人的系列")
        cls.books = []
        for i, user in enumerate([cls.owner, cls.owner, cls.other]):
            item = Item.objects.create(
                source="manual",
                media_type="book",
                media_id=f"series-{i}",
                title=f"星海 {i}",
                image="/static/none.svg",
            )
            record = Book(
                user=user, item=item, status="Completed", score=i + 7, notes="独立记录"
            )
            models.Model.save(record)
            cls.books.append(record)

    def setUp(self):
        self.client.force_login(self.owner)

    def member(self, record, **values):
        return self.client.post(
            "/library/series/",
            {
                "action": "member",
                "group": self.group.pk,
                "work": f"book:{record.pk}",
                "label": "第一卷",
                "sort_order": 1,
                **values,
            },
        )

    def test_group_create_update_and_delete_only_relationships(self):
        response = self.client.post(
            "/library/series/",
            {"action": "create", "name": "不同译本", "kind": "edition"},
        )
        self.assertEqual(response.status_code, 302)
        group = LibrarySeries.objects.get(user=self.owner, name="不同译本")
        self.member(self.books[0], group=group.pk)
        response = self.client.post(
            "/library/series/",
            {
                "action": "edit",
                "group": group.pk,
                "name": "各语种译本",
                "kind": "edition",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.client.post("/library/series/", {"action": "delete", "group": group.pk})
        self.assertFalse(LibrarySeries.objects.filter(pk=group.pk).exists())
        self.books[0].refresh_from_db()
        self.assertEqual(self.books[0].score, 7)
        self.assertEqual(self.books[0].notes, "独立记录")

    def test_order_labels_multiple_groups_and_removal(self):
        self.member(self.books[0], sort_order=9)
        self.member(self.books[1], sort_order=2, label="第二卷")
        second = LibrarySeries.objects.create(user=self.owner, name="作者系列")
        self.member(self.books[0], group=second.pk)
        response = self.client.get("/library/series/", {"group": self.group.pk})
        self.assertEqual(
            [row["record"].pk for row in response.context["members"]],
            [self.books[1].pk, self.books[0].pk],
        )
        self.assertContains(response, "第二卷")
        self.assertContains(response, "8 / 10")
        self.member(self.books[0], sort_order=0, label="前传")
        member = self.group.members.get(item=self.books[0].item)
        self.assertEqual(member.sort_order, 0)
        self.client.post(
            "/library/series/",
            {"action": "remove", "group": self.group.pk, "member": member.pk},
        )
        self.assertTrue(second.members.filter(item=self.books[0].item).exists())
        self.assertEqual(Book.objects.filter(user=self.owner).count(), 2)

    def test_cross_owner_inputs_and_invalid_values_are_rejected(self):
        self.assertEqual(
            self.client.get("/library/series/", {"group": self.private.pk}).status_code,
            404,
        )
        self.assertEqual(
            self.member(self.books[0], group=self.private.pk).status_code, 404
        )
        self.assertEqual(self.member(self.books[2]).status_code, 400)
        self.assertEqual(self.member(self.books[0], sort_order=-1).status_code, 400)
        self.assertEqual(self.member(self.books[0], label="字" * 81).status_code, 400)
        self.assertEqual(
            self.client.get("/library/series/", {"group": "9" * 40}).status_code, 404
        )
        self.assertFalse(LibrarySeriesMember.objects.exists())
        self.assertNotContains(self.client.get("/library/series/"), "私人的系列")

    @patch("app.providers.services.get_media_metadata", return_value={})
    def test_detail_shows_owned_relationships_and_returns_to_group(self, metadata):
        self.member(self.books[0])
        LibrarySeriesMember.objects.create(series=self.private, item=self.books[0].item)
        destination = f"/library/series/?group={self.group.pk}"
        response = self.client.get(
            f"/library/book/{self.books[0].pk}/", {"next": destination}
        )
        self.assertContains(response, "第一卷")
        self.assertNotContains(response, "私人的系列")
        self.assertEqual(response.context["return_url"], destination)

    def test_series_json_roundtrip_and_existing_memberships_preserved(self):
        self.member(self.books[0], label="前传", sort_order=3)
        self.member(self.books[1], label="第二卷", sort_order=10)
        empty = LibrarySeries.objects.create(
            user=self.owner, name="空的版本组", kind="edition"
        )
        payload = transfer.validate_payload(transfer.export_payload(self.owner))
        self.assertEqual(payload["version"], 2)
        self.assertEqual(len(payload["series"]), 2)
        self.assertEqual(transfer.import_payload(self.other, payload), 2)
        imported = LibrarySeries.objects.get(user=self.other, name=self.group.name)
        self.assertEqual(
            list(imported.members.values_list("label", "sort_order")),
            [("前传", 3), ("第二卷", 10)],
        )
        self.assertTrue(
            LibrarySeries.objects.filter(user=self.other, name=empty.name).exists()
        )
        before = list(imported.members.values_list("label", "sort_order"))
        payload["series"][0]["members"][0]["label"] = "不覆盖本人修改"
        self.assertEqual(transfer.import_payload(self.other, payload), 0)
        self.assertEqual(
            list(imported.members.values_list("label", "sort_order")), before
        )

    def test_prior_export_files_and_invalid_relationships(self):
        self.member(self.books[0])
        payload = transfer.export_payload(self.owner)
        previous = {key: value for key, value in payload.items() if key != "series"}
        previous["version"] = 1
        self.assertEqual(transfer.validate_payload(previous)["series"], [])
        for change in [
            {"media_id": "missing"},
            {"sort_order": -1},
            {"label": "字" * 81},
            {"source": []},
        ]:
            invalid = deepcopy(payload)
            invalid["series"][0]["members"][0].update(change)
            with self.assertRaises(ValidationError):
                transfer.validate_payload(invalid)
        self.assertFalse(LibraryFolder.objects.exists())
