"""Private reading positions and links remain independent of tracked progress."""

from decimal import Decimal
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase
from django.utils import timezone

from app import library
from app.models import Episode, Item, LibraryLink, Season


class LibraryPositionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="position-owner")
        cls.other = get_user_model().objects.create_user(username="position-other")

    def setUp(self):
        self.client.force_login(self.owner)
        metadata = patch("app.library.services.get_media_metadata", return_value={})
        metadata.start()
        self.addCleanup(metadata.stop)

    def record(self, kind="book", *, user=None, item=None, source="manual"):
        item = item or Item.objects.create(
            source=source,
            media_type=kind,
            media_id=f"position-{kind}-{Item.objects.count()}",
            title=f"位置测试{kind}",
        )
        values = {} if kind == "tv" else {"progress": 27}
        record = library.model_for(kind)(
            user=user or self.owner,
            item=item,
            score=Decimal("8.5"),
            status="Paused",
            notes="个人感想",
            **values,
        )
        models.Model.save(record)
        return record

    def save_position(self, record, **data):
        return self.client.post(
            f"/library/{record.item.media_type}/{record.pk}/",
            {
                "score": str(record.score),
                "status": record.status,
                "notes": record.notes,
                "position": "",
                "viewing_url": "",
                **data,
            },
        )

    def test_all_six_types_save_positions_and_offer_their_continuation_link(self):
        labels = {
            "book": "继续阅读",
            "manga": "继续阅读",
            "anime": "继续观看",
            "tv": "继续观看",
            "movie": "继续观看",
            "game": "打开链接",
        }
        for kind, label in labels.items():
            with self.subTest(kind=kind):
                record = self.record(kind)
                progress = record.progress
                url = f"https://example.org/{kind}/chapter-32"
                position = f"{kind} 第 32 章 / 第二季第 4 集"
                response = self.save_position(
                    record, position=position, viewing_url=url
                )
                self.assertEqual(response.status_code, 302)
                record.refresh_from_db()
                self.assertEqual(record.progress, progress)
                self.assertEqual(record.score, Decimal("8.5"))
                self.assertEqual(record.notes, "个人感想")
                link = LibraryLink.objects.get(user=self.owner, item=record.item)
                self.assertEqual((link.position, link.url), (position, url))
                detail = self.client.get(f"/library/{kind}/{record.pk}/")
                self.assertEqual(detail.context["form"]["position"].value(), position)
                self.assertEqual(detail.context["form"]["viewing_url"].value(), url)
                shelf = self.client.get("/library/", {"type": kind})
                soup = BeautifulSoup(shelf.content, "html.parser")
                self.assertEqual(soup.select_one(".work-position").text, position)
                resume = soup.select_one(".work-resume")
                self.assertEqual(resume["href"], url)
                self.assertIn(label, resume.text)
                self.assertIsNone(resume.find_parent("a"))

    def test_tv_position_does_not_change_computed_episode_progress_or_dates(self):
        record = self.record("tv")
        season_item = Item.objects.create(
            source="manual",
            media_type="season",
            media_id=record.item.media_id,
            title="第二季",
            season_number=2,
        )
        season = Season(
            user=self.owner, item=season_item, related_tv=record, status="In progress"
        )
        models.Model.save(season)
        episode_item = Item.objects.create(
            source="manual",
            media_type="episode",
            media_id=record.item.media_id,
            title="第四集",
            season_number=2,
            episode_number=4,
        )
        watched_at = timezone.now()
        episode = Episode(item=episode_item, related_season=season, end_date=watched_at)
        models.Model.save(episode)
        self.assertEqual(record.progress, 4)
        response = self.save_position(record, position="第二季第 8 集")
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.progress, 4)
        self.assertEqual(record.start_date, watched_at)
        self.assertEqual(record.end_date, watched_at)
        self.assertEqual(record.seasons.count(), 1)
        self.assertEqual(season.episodes.count(), 1)
        episode.refresh_from_db()
        self.assertEqual(episode.end_date, watched_at)

    def test_position_and_url_can_be_saved_and_cleared_independently(self):
        record = self.record()
        path = f"/library/book/{record.pk}/"
        self.assertEqual(
            self.save_position(record, position="第 10 章").status_code, 302
        )
        link = LibraryLink.objects.get(user=self.owner, item=record.item)
        self.assertEqual((link.position, link.url), ("第 10 章", ""))
        soup = BeautifulSoup(self.client.get("/library/").content, "html.parser")
        self.assertIsNone(soup.select_one(".work-resume"))
        self.assertEqual(
            self.save_position(
                record, position="", viewing_url="https://example.org/read"
            ).status_code,
            302,
        )
        link.refresh_from_db()
        self.assertEqual((link.position, link.url), ("", "https://example.org/read"))
        self.assertEqual(self.save_position(record).status_code, 302)
        self.assertFalse(
            LibraryLink.objects.filter(user=self.owner, item=record.item).exists()
        )
        self.assertEqual(self.client.get(path).context["form"]["position"].value(), "")
        soup = BeautifulSoup(self.client.get("/library/").content, "html.parser")
        self.assertIsNone(soup.select_one(".work-position"))
        self.assertIsNone(soup.select_one(".work-resume"))

    def test_steam_empty_link_and_position_remain_explicitly_cleared(self):
        record = self.record("game", source="steam")
        self.save_position(
            record, position="第二关", viewing_url="https://example.org/play"
        )
        self.assertEqual(self.save_position(record).status_code, 302)
        link = LibraryLink.objects.get(user=self.owner, item=record.item)
        self.assertEqual((link.position, link.url), ("", ""))
        detail = self.client.get(f"/library/game/{record.pk}/")
        self.assertEqual(detail.context["form"]["viewing_url"].value(), "")
        self.assertEqual(detail.context["form"]["position"].value(), "")

    def test_shared_work_has_private_positions_and_foreign_record_cannot_be_edited(
        self,
    ):
        own = self.record()
        foreign = self.record(user=self.other, item=own.item)
        private = LibraryLink.objects.create(
            user=self.other,
            item=own.item,
            position="他人的第 90 章",
            url="https://example.org/private",
        )
        self.assertEqual(
            self.save_position(own, position="我的第 3 章").status_code, 302
        )
        shelf = self.client.get("/library/")
        self.assertContains(shelf, "我的第 3 章")
        self.assertNotContains(shelf, private.position)
        self.assertNotContains(shelf, private.url)
        self.assertEqual(
            self.save_position(
                foreign, position="改动", viewing_url="https://example.org/edit"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/library/book/{foreign.pk}/").status_code, 404
        )
        private.refresh_from_db()
        self.assertEqual(
            (private.position, private.url),
            ("他人的第 90 章", "https://example.org/private"),
        )

    def test_invalid_url_and_long_position_leave_the_existing_values_unchanged(self):
        record = self.record()
        self.save_position(
            record, position="第 6 章", viewing_url="https://example.org/read"
        )
        for data in [
            {"viewing_url": "javascript:alert(1)"},
            {"viewing_url": "ftp://example.org/read"},
            {"viewing_url": "not a url"},
            {"position": "章" * 121},
        ]:
            with self.subTest(data=data):
                response = self.save_position(record, score="2", notes="改动", **data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                record.refresh_from_db()
                self.assertEqual(
                    (record.score, record.notes), (Decimal("8.5"), "个人感想")
                )
                link = LibraryLink.objects.get(user=self.owner, item=record.item)
                self.assertEqual(
                    (link.position, link.url), ("第 6 章", "https://example.org/read")
                )
