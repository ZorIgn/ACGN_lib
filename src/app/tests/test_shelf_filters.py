"""Combined shelf filters retain user boundaries and pagination conditions."""

from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase

from app.models import Book, Game, Item, Manga


class ShelfFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="filter-owner")
        cls.other = get_user_model().objects.create_user(username="filter-other")
        for model, kind, title, status, user in [
            (Book, "book", "海上的故事", "Completed", cls.owner),
            (Game, "game", "海上的游戏", "In progress", cls.owner),
            (Manga, "manga", "海上的漫画", "Completed", cls.owner),
            (Book, "book", "海上的续篇", "Paused", cls.owner),
            (Book, "book", "山中的故事", "Completed", cls.owner),
            (Book, "book", "未定的故事", "", cls.owner),
            (Book, "book", "海上的私藏", "Completed", cls.other),
        ]:
            item = Item.objects.create(
                source="manual",
                media_type=kind,
                media_id=title,
                title=title,
                image="/static/img/none.svg",
            )
            models.Model.save(model(user=user, item=item, status=status))

    def setUp(self):
        self.client.force_login(self.owner)

    def test_multiple_types_and_statuses_intersect_with_name(self):
        response = self.client.get(
            "/library/",
            {
                "q": "海上",
                "type": ["book", "game"],
                "status": ["Completed", "In progress"],
            },
        )
        entries = response.context["page"]
        self.assertEqual(
            {entry["record"].item.title for entry in entries},
            {"海上的故事", "海上的游戏"},
        )
        self.assertEqual(response.context["selected_kinds"], ["book", "game"])
        self.assertEqual(
            response.context["selected_states"], ["Completed", "In progress"]
        )
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(len(soup.select(".filter-panel input[checked]")), 4)

    def test_unset_and_completed_can_be_selected_together(self):
        response = self.client.get(
            "/library/", {"type": "book", "status": ["unset", "Completed"]}
        )
        self.assertEqual(
            {entry["record"].item.title for entry in response.context["page"]},
            {"未定的故事", "海上的故事", "山中的故事"},
        )

    def test_empty_filter_result_has_reset_action(self):
        response = self.client.get("/library/", {"type": "manga", "status": "Paused"})
        self.assertContains(response, "没有找到符合条件的作品")
        self.assertContains(response, "清除筛选")
        self.assertEqual(response.context["page"].paginator.count, 0)

    def test_all_and_unknown_conditions_keep_only_owned_records(self):
        for params in ({}, {"type": ["invalid"], "status": ["invalid"]}):
            response = self.client.get("/library/", params)
            self.assertEqual(response.context["page"].paginator.count, 6)
            self.assertNotContains(response, "海上的私藏")

    def test_pagination_keeps_every_filter_value(self):
        for index in range(40):
            item = Item.objects.create(
                source="manual",
                media_type="book",
                media_id=f"page-{index}",
                title=f"海上 {index}",
                image="/static/img/none.svg",
            )
            models.Model.save(Book(user=self.owner, item=item, status="Completed"))
        params = {
            "q": "海上",
            "type": ["book", "game"],
            "status": ["Completed", "In progress"],
        }
        response = self.client.get("/library/", params)
        soup = BeautifulSoup(response.content, "html.parser")
        url = soup.select_one(".shelf-pagination a")["href"]
        self.assertEqual(
            parse_qs(urlparse(url).query), {**params, "q": ["海上"], "page": ["2"]}
        )
        self.assertEqual(self.client.get("/library/" + url).context["page"].number, 2)
