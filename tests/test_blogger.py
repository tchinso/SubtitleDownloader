"""Regression cases for episode discovery on a creator's Blogger site."""

import unittest
from urllib.parse import urlencode

from subfinder.models import Anime, Query
from subfinder.network import SourceError
from subfinder.providers.reanime import ReAnime


TITLE = "책벌레의 하극상 영주의 양녀"
ORIGIN = "https://kairan03.blogspot.com"
LATEST = f"{ORIGIN}/2026/09/23.html"


def post(number):
    page = f"{ORIGIN}/2026/09/{number}.html"
    title = f"{TITLE} {number}화 자막"
    file_url = f"https://drive.google.com/file/d/episode_{number}/view"
    content = f'<a href="{file_url}">Google Drive 자막</a>'
    return {"title": {"$t": title}, "content": {"$t": content},
            "link": [{"rel": "alternate", "href": page}]}


class FakeHttp:
    def __init__(self):
        self.json = {}
        self.html = {}
        self.calls = []

    def get_json(self, url, **_):
        self.calls.append(url)
        if url not in self.json:
            raise SourceError(f"missing fixture: {url}")
        return self.json[url]

    def get_text(self, url):
        self.calls.append(url)
        if url not in self.html:
            raise SourceError(f"missing fixture: {url}")
        return self.html[url]


def fixture(numbers=(2, 3, 22, 23)):
    http = FakeHttp()
    latest = post(23)
    http.html[LATEST] = (f'<meta property="og:title" content="{latest["title"]["$t"]}">'
                         + latest["content"]["$t"])
    feed_url = f"{ORIGIN}/feeds/posts/default?" + urlencode(
        {"alt": "json", "max-results": 100, "q": TITLE})
    http.json[feed_url] = {"feed": {"openSearch$totalResults": {"$t": str(len(numbers))},
                                    "entry": [post(n) for n in numbers]}}
    http.json["https://api.anissia.net/anime/caption/animeNo/3291"] = {
        "data": [{"name": "카이란", "episode": 23, "website": LATEST}]}
    return http


class BloggerEpisodeTests(unittest.TestCase):
    def test_selected_anime_finds_each_episode_and_browse_all(self):
        anime = Anime(3291, TITLE)
        for episode in (2, 3, 22, 23):
            with self.subTest(episode=episode):
                http = fixture()
                result = ReAnime(http).search_selected(Query(TITLE, episode=episode), anime)
                self.assertEqual(result.status, "found")
                self.assertEqual([item.episode for item in result.candidates], [episode])
                self.assertEqual(result.candidates[0].source_url,
                                 f"{ORIGIN}/2026/09/{episode}.html")
                self.assertFalse(any("www.google.com" in url for url in http.calls))

        http = fixture()
        result = ReAnime(http).search_selected(Query(TITLE), anime)
        self.assertEqual({item.episode for item in result.candidates}, {2, 3, 22, 23})
        self.assertEqual(len(result.candidates), 4)
        self.assertFalse(any("www.google.com" in url for url in http.calls))

    def test_feed_pagination_includes_episodes_after_first_hundred(self):
        http = fixture(range(1, 102))
        first = f"{ORIGIN}/feeds/posts/default?" + urlencode(
            {"alt": "json", "max-results": 100, "q": TITLE})
        second = f"{ORIGIN}/feeds/posts/default?" + urlencode(
            {"alt": "json", "max-results": 100, "q": TITLE, "start-index": 101})
        http.json[first] = {"feed": {"openSearch$totalResults": {"$t": "101"},
                                     "entry": [post(n) for n in range(1, 101)]}}
        http.json[second] = {"feed": {"openSearch$totalResults": {"$t": "101"},
                                      "entry": [post(101)]}}
        result = ReAnime(http)._blogger(ORIGIN, [TITLE], Query(TITLE), "카이란")
        self.assertEqual(len(result), 101)
        self.assertEqual({item.episode for item in result}, set(range(1, 102)))

    def test_combined_episode_post_is_visible_for_both_episodes(self):
        http = FakeHttp()
        feed = f"{ORIGIN}/feeds/posts/default?" + urlencode(
            {"alt": "json", "max-results": 100, "q": TITLE})
        http.json[feed] = {"feed": {"entry": [{
            "title": {"$t": f"{TITLE} 10~11화 자막"},
            "content": {"$t": '<a href="https://drive.google.com/file/d/combined_1011/view">자막</a>'},
            "link": [{"rel": "alternate", "href": f"{ORIGIN}/2026/06/1011.html"}],
        }]}}
        browser = ReAnime(http)._blogger(ORIGIN, [TITLE], Query(TITLE), "카이란")
        self.assertEqual({item.episode for item in browser}, {10, 11})
        self.assertEqual(len({item.key for item in browser}), 2)
        for episode in (10, 11):
            with self.subTest(episode=episode):
                match = ReAnime(http)._blogger(ORIGIN, [TITLE], Query(TITLE, episode=episode), "카이란")
                self.assertEqual(len(match), 1)
                self.assertEqual(match[0].confidence, "review")
                self.assertTrue(match[0].require_episode)

if __name__ == "__main__":
    unittest.main()
