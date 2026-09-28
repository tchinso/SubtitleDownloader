import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from subfinder.gui import App
from subfinder.models import Candidate, Query, SearchResult


class SearchControlsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.app = App(self.root)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_three_primary_buttons_are_source_specific(self):
        self.assertEqual(self.app.search_button.cget("text"), "애니시아 작품 찾기")
        self.assertEqual(self.app.bigfile_button.cget("text"), "Bigfile 검색 (영문)")
        self.assertEqual(self.app.open_button.cget("text"), "OpenSubtitles 검색 (영문)")

        self.app.title.set("책벌레의 하극상")
        self.app.language.set("en")
        with patch.object(self.app, "_run") as run, patch.object(self.app.reanime, "discover") as discover:
            self.app.search()
            self.assertEqual(run.call_args.args[1]()[0], "anime")
            discover.assert_called_once_with("책벌레의 하극상")

    def test_google_search_opens_only_when_user_asks(self):
        self.app.title.set("선택 작품")
        self.app.episode.set("7")
        expected = "https://www.google.com/search?hl=ko&q=%EC%84%A0%ED%83%9D+%EC%9E%91%ED%92%88+7%ED%99%94+%EC%9E%90%EB%A7%89"
        with patch("subfinder.gui.webbrowser.open") as open_browser:
            self.app.google_open()
            open_browser.assert_called_once_with(expected)

    def test_opensubtitles_uses_english_title_without_merging_previous_results(self):
        self.app.title.set("책벌레의 하극상")
        self.app.episode.set("22")
        self.app.language.set("en")
        self.app.results = SearchResult([Candidate("ReAnime", "old", "old.ass", "https://example.com")])
        with patch("subfinder.gui.simpledialog.askstring", return_value="Ascendance of a Bookworm"), \
                patch.object(self.app, "_run") as run, \
                patch.object(self.app.engine, "search_open", return_value=SearchResult()) as search_open:
            self.app.search_open()
            self.assertEqual(run.call_args.args[1]()[0], "search")
            search_open.assert_called_once_with(Query("Ascendance of a Bookworm", language="en", episode=22))
        self.assertEqual(self.app.title.get(), "책벌레의 하극상")

    def test_bigfile_uses_english_title_and_opens_login_site_for_download(self):
        self.app.title.set("책벌레의 하극상")
        self.app.episode.set("22")
        with patch("subfinder.gui.simpledialog.askstring", return_value="Ascendance of a Bookworm"), \
                patch.object(self.app, "_run") as run, \
                patch.object(self.app.engine, "search_bigfile", return_value=SearchResult()) as search_bigfile:
            self.app.search_bigfile()
            self.assertEqual(run.call_args.args[1]()[0], "search")
            search_bigfile.assert_called_once_with(self.app.current_query)
        self.assertEqual(self.app.current_query, Query("Ascendance of a Bookworm", episode=22))
        self.assertEqual(self.app.title.get(), "책벌레의 하극상")
        candidate = Candidate("Bigfile", "Ascendance of a Bookworm", "Bookworm 22.smi",
                              "https://www.bigfile.co.kr/content/freecaption.php?cateGory=0005")
        self.app.results = SearchResult([candidate], status="found")
        self.app._populate()
        self.app.tree.selection_set("0")
        with patch("subfinder.gui.webbrowser.open") as open_browser, \
                patch.object(self.app.downloader, "download") as download:
            self.app.download()
        open_browser.assert_called_once_with(candidate.source_url)
        download.assert_not_called()

    def test_subtitle_double_click_downloads_clicked_row(self):
        self.app.current_query = Query("예시")
        candidates = [
            Candidate("ReAnime", "예시", "01.srt", "https://example.com/1", "https://example.com/1.srt"),
            Candidate("ReAnime", "예시", "02.srt", "https://example.com/2", "https://example.com/2.srt"),
        ]
        self.app.results = SearchResult(candidates, status="found")
        self.app._populate()
        self.app.tree.selection_set("0")
        with patch.object(self.app.tree, "identify_row", return_value="1"), \
                patch.object(self.app, "_run") as run, \
                patch.object(self.app.downloader, "download", return_value=("saved", "")) as downloader:
            self.app._download_on_double_click(SimpleNamespace(y=20))
            self.assertEqual(run.call_args.args[1]()[0], "download")
        self.assertEqual(self.app.tree.selection(), ("1",))
        downloader.assert_called_once_with(candidates[1], self.app.current_query)
        self.assertEqual(run.call_args.kwargs["error_title"], "다운로드 실패")

    def test_subtitle_context_menu_selects_clicked_row(self):
        self.app.results = SearchResult([
            Candidate("ReAnime", "예시", "01.srt", "https://example.com/1"),
            Candidate("ReAnime", "예시", "02.srt", "https://example.com/2"),
        ], status="found")
        self.app._populate()
        with patch.object(self.app.tree, "identify_row", return_value="1"), \
                patch.object(self.app.candidate_menu, "tk_popup") as popup:
            self.app._candidate_context_menu(SimpleNamespace(y=20, x_root=30, y_root=40))
        self.assertEqual(self.app.tree.selection(), ("1",))
        popup.assert_called_once_with(30, 40)

    def test_download_failure_is_shown_in_dialog(self):
        self.app.events.put((self.app.generation, "error", ("다운로드 실패", "자막 대신 웹페이지를 받음")))
        with patch("subfinder.gui.messagebox.showerror") as error:
            self.app._poll()
        self.assertIn("웹페이지", self.app.status.get())
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0], "다운로드 실패")


if __name__ == "__main__":
    unittest.main()
