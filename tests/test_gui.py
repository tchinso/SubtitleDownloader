import tkinter as tk
import unittest
from unittest.mock import patch

from subfinder.gui import App
from subfinder.models import Candidate, Query, SearchResult


class GoogleFailureHelpTests(unittest.TestCase):
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

    def test_failed_google_search_shows_url_for_submitted_query(self):
        self.app.current_query = Query("선택 작품", episode=7)
        self.app.title.set("나중에 입력한 작품")
        self.app.episode.set("9")
        result = SearchResult(status="error", warnings=[
            "Google 공개 검색: Google 검색 결과 형식을 읽지 못함. 브라우저에서 직접 확인해 줘"
        ])
        self.app.events.put((self.app.generation, "done", ("search", result)))

        self.app._poll()

        self.assertEqual(self.app.google_help.winfo_manager(), "pack")
        expected = "https://www.google.com/search?hl=ko&q=%EC%84%A0%ED%83%9D+%EC%9E%91%ED%92%88+7%ED%99%94+%EC%9E%90%EB%A7%89"
        self.assertEqual(self.app.google_url.get(), expected)
        self.assertIn("직접 열어 주세요", self.app.status.get())
        with patch("subfinder.gui.webbrowser.open") as open_browser:
            self.app.open_google_result()
            open_browser.assert_called_once_with(expected)
        self.app.copy_google_url()
        self.assertEqual(self.root.clipboard_get(), expected)

        self.app.events.put((self.app.generation, "done", ("search", SearchResult(status="empty"))))
        self.app._poll()
        self.assertEqual(self.app.google_help.winfo_manager(), "")

    def test_bigfile_uses_english_title_and_opens_login_site_for_download(self):
        self.app.title.set("책벌레의 하극상")
        self.app.episode.set("22")
        with patch("subfinder.gui.simpledialog.askstring", return_value="Ascendance of a Bookworm"), \
                patch.object(self.app, "_run") as run:
            self.app.search_bigfile()
        self.assertEqual(self.app.current_query, Query("Ascendance of a Bookworm", episode=22))
        run.assert_called_once()
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


if __name__ == "__main__":
    unittest.main()
