"""Selected Anissia works should link to creator blog home pages."""

import tkinter as tk
import unittest
from unittest.mock import patch

from subfinder.gui import App
from subfinder.models import Anime, AnimeSearchResult, CreatorBlog
from subfinder.network import SourceError
from subfinder.providers.reanime import ReAnime


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        return self.response


class CreatorBlogTests(unittest.TestCase):
    def test_latest_caption_link_opens_blog_home_not_latest_episode(self):
        http = FakeHttp({"data": [{
            "name": "카이란", "episode": 23,
            "website": "https://kairan03.blogspot.com/2026/09/bookworm-23.html",
        }]})
        blogs = ReAnime(http).creator_blogs(Anime(3291, "책벌레의 하극상 영주의 양녀"))
        self.assertEqual(blogs, [CreatorBlog("카이란", "https://kairan03.blogspot.com/")])
        self.assertEqual(http.calls, ["https://api.anissia.net/anime/caption/animeNo/3291"])

    def test_multiple_blog_hosts_are_normalized_and_deduplicated(self):
        http = FakeHttp({"data": [
            {"name": "첫 제작자", "website": "http://blog.naver.com/PostView.naver?blogId=maker&logNo=123"},
            {"name": "중복", "website": "https://blog.naver.com/maker/456"},
            {"name": "둘째 제작자", "website": "https://other.tistory.com/entry/episode-22"},
            {"name": "무효", "website": "https://127.0.0.1/private"},
        ]})
        blogs = ReAnime(http).creator_blogs(Anime(12, "예시"))
        self.assertEqual(blogs, [CreatorBlog("첫 제작자", "https://blog.naver.com/maker"),
                                 CreatorBlog("둘째 제작자", "https://other.tistory.com/")])

    def test_invalid_caption_response_is_reported(self):
        with self.assertRaises(SourceError):
            ReAnime(FakeHttp({"data": {}})).creator_blogs(Anime(12, "예시"))


class CreatorBlogGuiTests(unittest.TestCase):
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

    def test_selected_anime_blog_button_works_without_subtitle_results(self):
        anime = Anime(3291, "책벌레의 하극상 영주의 양녀")
        self.app.anime_results = AnimeSearchResult([anime], status="found")
        self.app._populate_anime()
        self.app.anime_tree.selection_set(str(anime.anime_no))
        self.app._select_anime()
        self.assertEqual(str(self.app.creator_blog_button["state"]), "normal")
        with patch.object(self.app, "_run") as run:
            self.app.open_creator_blog()
        self.assertIn("제작자 블로그", run.call_args.args[0])
        with patch("subfinder.gui.webbrowser.open") as open_browser:
            self.app.events.put((self.app.generation, "done", ("blogs", [
                CreatorBlog("카이란", "https://kairan03.blogspot.com/")
            ])))
            self.app._poll()
        open_browser.assert_called_once_with("https://kairan03.blogspot.com/")


if __name__ == "__main__":
    unittest.main()
