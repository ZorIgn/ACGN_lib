"""Steam synchronization keeps personal choices while refreshing public data."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.test import SimpleTestCase, TestCase

from app.library_tasks import sync_steam
from app.models import Game, Item, LibraryLink, SteamConnection
from app.providers import steam
from integrations.imports.helpers import encrypt


class SteamLibraryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="steam-owner")
        SteamConnection.objects.create(
            user=cls.owner,
            steam_id="76561198000000000",
            encrypted_key=encrypt("a" * 32),
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def sync(self, minutes=120, images=None):
        games = [{"appid": 123, "name": "A game", "playtime_forever": minutes}]
        with (
            patch("app.library_tasks.fetch_games", return_value=games),
            patch(
                "app.library_tasks.steam.covers",
                return_value=images or {},
            ),
        ):
            return sync_steam(self.owner.pk)

    def test_played_games_start_in_progress_with_store_link_and_art(self):
        self.sync(images={"123": "https://example.org/library.jpg"})
        record = Game.objects.get()
        self.assertEqual(record.status, "In progress")
        self.assertEqual(record.progress, 120)
        self.assertIsNone(record.start_date)
        self.assertIsNone(record.end_date)
        self.assertEqual(record.item.image, "https://example.org/library.jpg")
        self.assertEqual(LibraryLink.objects.get().url, steam.store_url("123"))

    def test_untouched_unplayed_game_moves_to_in_progress_on_first_play(self):
        self.sync(minutes=0)
        self.assertEqual(Game.objects.get().status, "Planning")
        self.sync(minutes=15)
        self.assertEqual(Game.objects.get().status, "In progress")

    def test_explicit_statuses_and_dates_are_preserved(self):
        self.sync(minutes=0)
        record = Game.objects.get()
        for status in ["Planning", "Paused", "Completed", "", "Dropped"]:
            record.status = status
            models.Model.save(record, update_fields=["status"])
            self.sync(minutes=200)
            record.refresh_from_db()
            self.assertEqual(record.status, status)
            self.assertIsNone(record.end_date)

    def test_sync_refreshes_artwork_without_overwriting_custom_or_empty_link(self):
        self.sync()
        self.assertEqual(Game.objects.get().item.image, settings.IMG_NONE)
        link = LibraryLink.objects.get()
        for url in ["https://example.org/my-game", ""]:
            link.url = url
            link.save()
            self.sync(images={"123": "https://example.org/current.jpg"})
            link.refresh_from_db()
            self.assertEqual(link.url, url)
            self.assertEqual(
                Game.objects.get().item.image, "https://example.org/current.jpg"
            )
        self.sync()
        self.assertEqual(
            Game.objects.get().item.image, "https://example.org/current.jpg"
        )

    def test_detail_has_steam_source_and_editable_default_link(self):
        self.sync()
        record = Game.objects.get()
        LibraryLink.objects.all().delete()
        path = f"/library/game/{record.pk}/"
        response = self.client.get(path)
        self.assertContains(response, "在 Steam 中查看")
        self.assertEqual(
            response.context["form"]["viewing_url"].value(), steam.store_url("123")
        )
        self.assertEqual(
            self.client.post(
                path,
                {
                    "status": "In progress",
                    "score": "",
                    "notes": "",
                    "viewing_url": "",
                },
            ).status_code,
            302,
        )
        self.sync()
        self.assertEqual(
            self.client.get(path).context["form"]["viewing_url"].value(), ""
        )
        self.assertContains(self.client.get(path), "在 Steam 中查看")

    def test_data_alignment_only_updates_unedited_played_steam_records(self):
        records = []
        for index, (source, edited, minutes) in enumerate(
            [
                ("steam", False, 90),
                ("steam", True, 90),
                ("steam", False, 0),
                ("manual", False, 90),
            ]
        ):
            item = Item.objects.create(
                source=source,
                media_type="game",
                media_id=str(index),
                title=f"Game {index}",
                image=settings.IMG_NONE,
            )
            record = Game(user=self.owner, item=item, status="Paused", progress=minutes)
            models.Model.save(record)
            if edited:
                models.Model.save(record, update_fields=["status"])
                LibraryLink.objects.create(
                    user=self.owner, item=item, url="https://example.org/custom"
                )
            records.append(record)
        migrate = importlib.import_module(
            "app.migrations.0067_steam_records"
        ).align_steam_records
        for _ in range(2):
            migrate(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        for record, expected in zip(
            records, ["In progress", "Paused", "Paused", "Paused"], strict=True
        ):
            record.refresh_from_db()
            self.assertEqual(record.status, expected)
            self.assertEqual(record.notes, "")
        self.assertEqual(LibraryLink.objects.count(), 3)
        self.assertEqual(
            LibraryLink.objects.get(item=records[1].item).url,
            "https://example.org/custom",
        )


class SteamArtworkTests(SimpleTestCase):
    def test_batch_reads_hashed_portraits_and_header_fallback(self):
        response = Mock()
        response.json.return_value = {
            "response": {
                "store_items": [
                    {
                        "appid": 1,
                        "assets": {
                            "asset_url_format": "steam/apps/1/${FILENAME}?t=123",
                            "library_capsule": "hash/library_600x900.jpg",
                            "header": "header.jpg",
                        },
                    },
                    {
                        "appid": 2,
                        "assets": {
                            "asset_url_format": "steam/apps/2/${FILENAME}",
                            "header": "hash/header.jpg",
                        },
                    },
                    {"appid": 3, "assets": {}},
                ]
            }
        }
        with patch("app.providers.steam.requests.get", return_value=response) as get:
            images = steam.covers([1, 1, *range(2, 52)])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            len(
                json.loads(get.call_args_list[0].kwargs["params"]["input_json"])["ids"]
            ),
            50,
        )
        self.assertEqual(
            len(
                json.loads(get.call_args_list[1].kwargs["params"]["input_json"])["ids"]
            ),
            1,
        )
        self.assertTrue(images["1"].endswith("/1/hash/library_600x900.jpg?t=123"))
        self.assertTrue(images["2"].endswith("/2/hash/header.jpg"))
        self.assertNotIn("3", images)

    def test_cover_timeout_and_invalid_payload_allow_sync_without_artwork(self):
        with patch("app.providers.steam.requests.get", side_effect=requests.Timeout):
            self.assertEqual(steam.covers([1]), {})
        response = Mock()
        response.json.return_value = {"response": None}
        with patch("app.providers.steam.requests.get", return_value=response):
            self.assertEqual(steam.covers([1]), {})
