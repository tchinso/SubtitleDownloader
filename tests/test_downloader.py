"""Download-time episode checks for results found without an episode filter."""

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from subfinder.downloader import Downloader
from subfinder.models import Candidate, Query
from subfinder.network import Response, SourceError


class FakeHttp:
    def __init__(self, response: Response):
        self.response = response

    def get_bytes(self, url: str, limit: int):
        return self.response


class BrowseEpisodeDownloadTests(unittest.TestCase):
    URL = "https://blog.kakaocdn.net/file/bookworm.zip"

    def _candidate(self) -> Candidate:
        return Candidate("ReAnime/Blogger", "Bookworm 22화 자막", "bookworm.zip",
                         "https://maker.blogspot.com/2026/09/bookworm.html",
                         self.URL, episode=22, require_episode=True)

    def test_selected_episode_extracts_correct_file_from_archive(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr("Bookworm 22.srt", b"Episode 22")
            target.writestr("Bookworm 23.srt", b"Episode 23")
        response = Response(archive.getvalue(), self.URL, {})

        with tempfile.TemporaryDirectory() as folder:
            path, note = Downloader(Path(folder), FakeHttp(response)).download(
                self._candidate(), Query("Bookworm"))

            self.assertEqual(path.read_bytes(), b"Episode 22")
            self.assertEqual(note, "")
            metadata = json.loads(path.with_name(path.name + ".source.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["episode"], 22)
            self.assertEqual(metadata["original_filename"], "Bookworm 22.srt")

    def test_selected_episode_rejects_mismatched_plain_file(self):
        response = Response(b"1\n00:00:01,000 --> 00:00:02,000\nWrong episode",
                            self.URL, {"Content-Disposition": 'attachment; filename="Bookworm 23.srt"'})

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SourceError, "화 정보가 요청과 다름"):
                Downloader(Path(folder), FakeHttp(response)).download(self._candidate(), Query("Bookworm"))


if __name__ == "__main__":
    unittest.main()
