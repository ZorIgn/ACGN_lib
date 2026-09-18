"""User-defined folders preserve record ownership and existing filters."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase

from app.models import Book, Game, Item, LibraryFolder


class FolderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="folder-owner")
        cls.other = get_user_model().objects.create_user(username="folder-other")
        cls.folder = LibraryFolder.objects.create(user=cls.owner, name="科幻")
        cls.second = LibraryFolder.objects.create(user=cls.owner, name="最爱")
        cls.private = LibraryFolder.objects.create(user=cls.other, name="私人分类")
        cls.item = Item.objects.create(
            source="manual",
            media_type="book",
            media_id="folder-book",
            title="星海",
            image="/static/none.svg",
        )
        cls.book = Book(user=cls.owner, item=cls.item, status="Completed", score=9)
        models.Model.save(cls.book)
        cls.folder.items.add(cls.item)
        cls.private.items.add(cls.item)
        game = Item.objects.create(
            source="manual",
            media_type="game",
            media_id="folder-game",
            title="星海游戏",
            image="/static/none.svg",
        )
        models.Model.save(Game(user=cls.owner, item=game, status="In progress"))
        cls.folder.items.add(game)

    def setUp(self):
        self.client.force_login(self.owner)

    def detail_post(self, **kwargs):
        return self.client.post(
            f"/library/book/{self.book.pk}/",
            {"status": "Completed", "score": "9", **kwargs},
        )

    def test_membership_can_be_multiple_and_cleared_without_affecting_other_users(self):
        response = self.detail_post(
            folders=[self.folder.pk, self.second.pk],
            next="/library/?folder=1&type=book&page=2",
        )
        self.assertEqual(response.url, "/library/?folder=1&type=book&page=2")
        self.assertEqual(
            LibraryFolder.objects.filter(user=self.owner, items=self.item).count(), 2
        )
        self.detail_post(folders=[])
        self.assertFalse(
            LibraryFolder.objects.filter(user=self.owner, items=self.item).exists()
        )
        self.assertTrue(self.private.items.filter(pk=self.item.pk).exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.score, 9)

    @patch("app.library.services.get_media_metadata", return_value={})
    def test_another_users_folder_cannot_be_viewed_changed_or_assigned(self, metadata):
        self.assertEqual(
            self.client.get("/library/", {"folder": self.private.pk}).status_code, 404
        )
        for action in ["rename", "delete"]:
            self.assertEqual(
                self.client.post(
                    "/library/folders/",
                    {"action": action, "folder_id": self.private.pk, "name": "改名"},
                ).status_code,
                404,
            )
        response = self.detail_post(folders=[self.private.pk], score="1")
        self.assertContains(response, "选择", status_code=200)
        self.book.refresh_from_db()
        self.assertEqual(self.book.score, 9)
        self.assertNotContains(
            self.client.get(f"/library/book/{self.book.pk}/"), "私人分类"
        )

    def test_folder_creation_rename_and_delete_preserve_media(self):
        payload = {"action": "create", "name": " 新分类 "}
        self.client.post("/library/folders/", payload)
        self.client.post("/library/folders/", payload)
        folder = LibraryFolder.objects.get(user=self.owner, name="新分类")
        response = self.client.post(
            "/library/folders/",
            {"action": "rename", "folder_id": folder.pk, "name": "重命名"},
        )
        self.assertEqual(response.url, f"/library/?folder={folder.pk}")
        folder.refresh_from_db()
        self.assertEqual(folder.name, "重命名")
        folder.items.add(self.item)
        self.client.post(
            "/library/folders/", {"action": "delete", "folder_id": folder.pk}
        )
        self.assertFalse(LibraryFolder.objects.filter(pk=folder.pk).exists())
        self.assertTrue(Book.objects.filter(pk=self.book.pk).exists())
        self.assertTrue(Item.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(self.folder.items.filter(pk=self.item.pk).exists())

    def test_folder_filters_combine_with_type_status_and_text_and_survive_links(self):
        params = {
            "folder": self.folder.pk,
            "type": ["book", "game"],
            "status": "Completed",
            "q": "星海",
        }
        response = self.client.get("/library/", params)
        self.assertEqual(response.context["page"].paginator.count, 1)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(soup.select_one("[name=folder]")["value"], str(self.folder.pk))
        self.assertEqual(
            soup.select_one("[data-filter-reset]")["href"],
            f"/library/?folder={self.folder.pk}",
        )
        destination = parse_qs(
            urlsplit(soup.select_one("[data-record-link]")["href"]).query
        )["next"][0]
        self.assertEqual(
            parse_qs(urlsplit(destination).query)["type"], ["book", "game"]
        )
        self.assertIn(f"folder={self.folder.pk}", destination)
        empty = self.client.get("/library/", {**params, "q": "没有这个名字"})
        soup = BeautifulSoup(empty.content, "html.parser")
        self.assertEqual(
            soup.select_one(".btn-empty-action")["href"],
            f"/library/?folder={self.folder.pk}",
        )

    def test_duplicate_or_invalid_folder_names_do_not_change_existing_folder(self):
        for value in ["", " " * 4, "长" * 81, self.second.name]:
            response = self.client.post(
                "/library/folders/",
                {"action": "rename", "folder_id": self.folder.pk, "name": value},
            )
            self.assertEqual(response.status_code, 302)
            self.folder.refresh_from_db()
            self.assertEqual(self.folder.name, "科幻")
