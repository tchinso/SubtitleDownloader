"""Download-time episode checks for results found without an episode filter."""

from io import BytesIO
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote_from_bytes
import zipfile
import zlib

from subfinder.downloader import Downloader, _disposition_name, _seven_zip_selection, _zip_filename
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


class FilenameEncodingTests(unittest.TestCase):
    NAME = "혼즈키4 10화.ass"
    URL = "https://blog.kakaocdn.net/file/bookworm.zip"
    BODY = b"[Script Info]\nTitle: Bookworm\n[Events]\n"

    def _download(self, body, headers=None):
        response = Response(body, self.URL, headers or {})
        candidate = Candidate("ReAnime/Blogger", "책벌레의 하극상 10화 자막", "bookworm.zip",
                              "https://maker.blogspot.com/2026/09/bookworm.html", self.URL,
                              episode=10, require_episode=True)
        with tempfile.TemporaryDirectory() as folder:
            path, note = Downloader(Path(folder), FakeHttp(response)).download(
                candidate, Query("책벌레의 하극상 영주의 양녀"))
            metadata = json.loads(path.with_name(path.name + ".source.json").read_text(encoding="utf-8"))
            return path.name, path.read_bytes(), note, metadata

    def test_raw_utf8_header_produces_readable_filename_and_preserves_body(self):
        # The real Google Drive response exposes these UTF-8 bytes as Latin-1.
        raw_name = self.NAME.encode("utf-8").decode("latin-1")
        name, body, note, metadata = self._download(
            self.BODY, {"Content-Disposition": f'attachment; filename="{raw_name}"'})
        self.assertEqual(name, self.NAME)
        self.assertEqual(body, self.BODY)
        self.assertEqual(note, "")
        self.assertEqual(metadata["original_filename"], self.NAME)

    def test_extended_filename_takes_priority_over_ascii_fallback(self):
        encoded = quote_from_bytes(self.NAME.encode("utf-8"))
        headers = {"content-disposition":
                   f'attachment; filename="fallback.ass"; filename*=UTF-8\'\'{encoded}'}
        self.assertEqual(_disposition_name(headers), self.NAME)

    def test_extended_filename_preserves_literal_percent_sequences(self):
        self.assertEqual(_disposition_name({"Content-Disposition":
            "attachment; filename*=UTF-8''100%2520.ass"}), "100%20.ass")

    def test_legacy_header_and_percent_encoded_names_are_decoded(self):
        for encoding in ("utf-8", "cp949"):
            for encoded in (self.NAME.encode(encoding).decode("latin-1"),
                            quote_from_bytes(self.NAME.encode(encoding))):
                with self.subTest(encoding=encoding, encoded=ascii(encoded)):
                    self.assertEqual(_disposition_name(
                        {"Content-Disposition": f'attachment; filename="{encoded}"'}), self.NAME)

    def test_invalid_extended_encoding_uses_plain_filename(self):
        for extended in ("unknown-charset''%FF.ass", "UTF-8''%FF.ass"):
            with self.subTest(extended=extended):
                self.assertEqual(_disposition_name({"Content-Disposition":
                    f'attachment; filename="fallback.ass"; filename*={extended}'}), "fallback.ass")

    def test_valid_unicode_and_western_names_are_preserved(self):
        for name in (self.NAME, "München 10.ass", "café 10.ass", "plain 10.ass"):
            with self.subTest(name=name):
                self.assertEqual(_disposition_name(
                    {"Content-Disposition": f'attachment; filename="{name}"'}), name)

    def test_korean_zip_members_without_utf8_flag_are_selected_and_named_correctly(self):
        for encoding in ("cp949", "utf-8"):
            class LegacyZipInfo(zipfile.ZipInfo):
                def _encodeFilenameFlags(self):
                    return self.filename.encode(encoding), self.flag_bits & ~0x800

            archive = BytesIO()
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr(LegacyZipInfo(self.NAME), self.BODY)
                target.writestr(LegacyZipInfo("혼즈키4 11화.ass"), b"Wrong episode")
            with self.subTest(encoding=encoding):
                name, body, note, metadata = self._download(archive.getvalue())
                self.assertEqual(name, self.NAME)
                self.assertEqual(body, self.BODY)
                self.assertEqual(note, "")
                self.assertEqual(metadata["original_filename"], self.NAME)
                self.assertEqual(metadata["episode"], 10)

    def test_zip_unicode_path_extra_works_with_older_python_and_checks_crc(self):
        raw = b"legacy 10.ass"
        info = zipfile.ZipInfo(raw.decode("cp437"))
        field = b"\x01" + struct.pack("<I", zlib.crc32(raw)) + self.NAME.encode("utf-8")
        info.extra = struct.pack("<HH", 0x7075, len(field)) + field
        self.assertEqual(_zip_filename(info), self.NAME)
        # A mismatched Unicode extra field must not rename another entry.
        field = b"\x01" + struct.pack("<I", zlib.crc32(raw) ^ 1) + self.NAME.encode("utf-8")
        info.extra = struct.pack("<HH", 0x7075, len(field)) + field
        self.assertEqual(_zip_filename(info), "legacy 10.ass")

    def test_ordinary_western_zip_names_are_not_mistaken_for_cp949(self):
        for name in ("école 10.ass", "München 10.ass", "éà 10.ass"):
            with self.subTest(name=name):
                self.assertEqual(_zip_filename(zipfile.ZipInfo(name)), name)

    def test_7zip_listing_requests_utf8_and_retains_korean_member_name(self):
        listing = f"Path = {self.NAME}\nSize = {len(self.BODY)}\nFolder = -\n\n"
        outputs = [SimpleNamespace(returncode=0, stdout=listing.encode("utf-8")),
                   SimpleNamespace(returncode=0, stdout=self.BODY)]
        with patch("subfinder.downloader.shutil.which", return_value="7z"), \
                patch("subfinder.downloader.subprocess.run", side_effect=outputs) as run:
            self.assertEqual(_seven_zip_selection(b"archive", ".7z", 10, require_episode=True),
                             (self.BODY, self.NAME))
        self.assertIn("-sccUTF-8", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0][-1], self.NAME)


if __name__ == "__main__":
    unittest.main()
