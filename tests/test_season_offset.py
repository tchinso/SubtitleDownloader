"""Cumulative Tistory numbering stays reviewable and downloads the source episode."""

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote
import zipfile

from subfinder.downloader import Downloader
from subfinder.models import Anime, Query
from subfinder.network import Response, SourceError
from subfinder.providers.reanime import ReAnime


class FakeHttp:
    def __init__(self, latest_title: str = "예시 작품 14화 자막", registered_latest=2):
        self.source = "https://maker.tistory.com/entry/latest"
        self.attachment = "https://blog.kakaocdn.net/file/all.zip"
        self.json = {
            "https://api.anissia.net/anime/caption/animeNo/12": {
                "data": [{"name": "Maker", "website": self.source,
                          "episode": registered_latest}]}}
        self.html = {
            self.source: f'<meta property="og:title" content="{latest_title}">',
            "https://maker.tistory.com/search/" + quote("예시 작품 2기", safe=""): "<html></html>",
            "https://maker.tistory.com/search/" + quote("예시 작품", safe=""): (
                '<a href="/entry/older" data-tiara-copy="예시 작품 13화 자막">글 읽기</a>'),
            "https://maker.tistory.com/entry/older": (
                '<meta property="og:title" content="예시 작품 13화 자막">'
                f'<a href="{self.attachment}">자막</a>')}
        self.binary = {}

    def get_json(self, url, **options):
        if url not in self.json:
            raise SourceError("fixture not found: " + url)
        return self.json[url]

    def get_text(self, url):
        if url not in self.html:
            raise SourceError("fixture not found: " + url)
        return self.html[url]

    def get_bytes(self, url, limit=6 * 1024 * 1024):
        if url not in self.binary:
            raise SourceError("fixture not found: " + url)
        return self.binary[url]


class SeasonOffsetTests(unittest.TestCase):
    anime = Anime(12, "예시 작품 2기")
    query = Query("예시 작품 2기", season=2, episode=1)

    def test_confirmed_latest_number_produces_review_only_candidate(self):
        http = FakeHttp()

        result = ReAnime(http).search_selected(self.query, self.anime)

        self.assertEqual(result.status, "review")
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.source_url, "https://maker.tistory.com/entry/older")
        self.assertEqual((candidate.season, candidate.episode, candidate.source_episode), (2, 1, 13))
        self.assertEqual(candidate.confidence, "review")
        self.assertTrue(candidate.require_episode)
        self.assertIn("통산 13화", candidate.note)

    def test_unrelated_latest_post_cannot_establish_offset(self):
        http = FakeHttp(latest_title="다른 작품 14화 자막")

        result = ReAnime(http).search_selected(self.query, self.anime)

        self.assertEqual(result.candidates, [])

    def test_missing_registered_latest_cannot_establish_offset(self):
        http = FakeHttp(registered_latest=0)

        result = ReAnime(http).search_selected(self.query, self.anime)

        self.assertEqual(result.candidates, [])

    def test_download_selects_cumulative_file_but_records_season_episode(self):
        http = FakeHttp()
        candidate = ReAnime(http).search_selected(self.query, self.anime).candidates[0]
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("예시 작품 13.srt", b"source episode 13")
            archive.writestr("예시 작품 14.srt", b"source episode 14")
        http.binary[http.attachment] = Response(stream.getvalue(), http.attachment, {})

        with tempfile.TemporaryDirectory() as folder:
            path, note = Downloader(Path(folder), http).download(candidate, self.query)
            metadata = json.loads(path.with_name(path.name + ".source.json").read_text(encoding="utf-8"))

            self.assertEqual(path.read_bytes(), b"source episode 13")
            self.assertEqual(note, "")
            self.assertEqual(path.parent.name, "Season 02")
            self.assertTrue(path.name.startswith("S02E01 - "))
            self.assertEqual(metadata["episode"], 1)
            self.assertEqual(metadata["source_episode"], 13)
            self.assertEqual(metadata["original_filename"], "예시 작품 13.srt")

    def test_plain_file_uses_source_episode_for_filename_check(self):
        http = FakeHttp()
        candidate = ReAnime(http).search_selected(self.query, self.anime).candidates[0]
        candidate.file_name = "예시 작품 13.srt"
        http.binary[http.attachment] = Response(
            b"1\n00:00:01,000 --> 00:00:02,000\nHello", http.attachment,
            {"Content-Disposition": 'attachment; filename="Example 13.srt"'})
        with tempfile.TemporaryDirectory() as folder:
            path, _ = Downloader(Path(folder), http).download(candidate, self.query)
            self.assertTrue(path.is_file())

        http.binary[http.attachment] = Response(
            b"1\n00:00:01,000 --> 00:00:02,000\nWrong", http.attachment,
            {"Content-Disposition": 'attachment; filename="Example 12.srt"'})
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SourceError, "화 정보가 요청과 다름"):
                Downloader(Path(folder), http).download(candidate, self.query)

    def test_offset_candidate_rejects_a_changed_episode_query(self):
        http = FakeHttp()
        candidate = ReAnime(http).search_selected(self.query, self.anime).candidates[0]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SourceError, "현재 요청과 다름"):
                Downloader(Path(folder), http).download(
                    candidate, Query("예시 작품 2기", season=2, episode=2))

    def test_offset_candidate_keeps_confirmed_season_when_input_season_is_blank(self):
        http = FakeHttp()
        candidate = ReAnime(http).search_selected(self.query, self.anime).candidates[0]
        http.binary[http.attachment] = Response(
            b"1\n00:00:01,000 --> 00:00:02,000\nHello", http.attachment,
            {"Content-Disposition": 'attachment; filename="Example 13.srt"'})

        with tempfile.TemporaryDirectory() as folder:
            path, _ = Downloader(Path(folder), http).download(
                candidate, Query("예시 작품 2기", episode=1))
            metadata = json.loads(path.with_name(path.name + ".source.json").read_text(encoding="utf-8"))

            self.assertEqual(path.parent.name, "Season 02")
            self.assertEqual((metadata["season"], metadata["episode"], metadata["source_episode"]),
                             (2, 1, 13))


if __name__ == "__main__":
    unittest.main()
