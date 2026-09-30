"""Anissia's official title can be longer than a creator's confirmed title."""

from html import escape
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, quote, urlencode, urlsplit
import zipfile

from subfinder.downloader import Downloader
from subfinder.models import Anime, Query
from subfinder.network import Response, SourceError
from subfinder.providers.reanime import ReAnime


SHORT = "아가씨 돌보기"
LONG = (SHORT + " ~~영애들이 다니는 명문 학교에서 제일가는 아가씨(생활력 없음)를 "
        "남몰래 돕는 시중 담당이 되었습니다~~")
EXTENDED = SHORT + " 영애들이 다니는 명문 학교에서 시중 담당이 되었습니다"
CAPTIONS = "https://api.anissia.net/anime/caption/animeNo/4200"


def subtitle(number):
    return f"1\n00:00:01,000 --> 00:00:02,000\nEpisode {number}\n".encode()


class FakeHttp:
    def __init__(self):
        self.pages = {}
        self.json = {}
        self.files = {}
        self.calls = []

    def get_text(self, url):
        self.calls.append(url)
        if url in self.pages:
            return self.pages[url]
        parsed = urlsplit(url)
        if parsed.path.startswith("/search/") or parsed.path in (
                "/search", "/PostSearchList.naver"):
            return "<html><body></body></html>"
        raise SourceError(f"Missing fixture: {url}")

    def get_json(self, url, **_):
        self.calls.append(url)
        if url in self.json:
            return self.json[url]
        if urlsplit(url).path == "/feeds/posts/default":
            return {"feed": {"entry": []}}
        raise SourceError(f"Missing fixture: {url}")

    def get_bytes(self, url, limit):
        self.calls.append(url)
        try:
            return self.files[url]
        except KeyError as exc:
            raise SourceError(f"Missing fixture: {url}") from exc


def add_blog(http, provider, *, maker="maker", title=SHORT,
             source_title=None, search_terms=None, distractors=False):
    """Only searches for the blog's short title expose the older posts."""
    if provider == "naver":
        origin = "https://blog.naver.com"
        page = lambda number: f"{origin}/{maker}/{number}"
        attachment = lambda number: f"https://download.blog.naver.com/{maker}/{number:02d}.srt"
        search = lambda term: origin + "/PostSearchList.naver?" + urlencode(
            {"blogId": maker, "SearchText": term})
    elif provider == "tistory":
        origin = f"https://{maker}.tistory.com"
        page = lambda number: f"{origin}/{number}"
        attachment = lambda number: f"https://blog.kakaocdn.net/file/{maker}/{number:02d}.srt"
        search = lambda term: f"{origin}/search/{quote(term, safe='')}"
    else:
        origin = f"https://{maker}.blogspot.com"
        page = lambda number: f"{origin}/2026/09/{number}.html"
        attachment = lambda number: f"https://drive.google.com/file/d/{maker}_{number}/view"
        search = lambda term: f"{origin}/feeds/posts/default?" + urlencode(
            {"alt": "json", "max-results": 100, "q": term})

    posts = [(number, f"{title} {number:02d}화 자막") for number in (1, 2, 3)]
    if distractors:
        posts.extend([(4, f"{title} 2기 02화 자막"),
                      (5, f"{title} OVA 02화 자막"),
                      (6, "관계없는 다른 작품 02화 자막")])
    entries = []
    links = []
    for number, post_title in posts:
        content = (f'<a href="{attachment(number)}" download="{number:02d}.srt">'
                   f'{number:02d}.srt</a>')
        visible_title = (source_title if number == 3 and source_title is not None
                         else post_title)
        http.pages[page(number)] = (
            f'<meta property="og:title" content="{escape(visible_title, quote=True)}">'
            f'<title>{escape(visible_title)}</title>{content}')
        entries.append({"title": {"$t": post_title}, "content": {"$t": content},
                        "link": [{"rel": "alternate", "href": page(number)}]})
        links.append(f'<a href="{page(number)}">{escape(post_title)}</a>')
        file_url = attachment(number)
        if provider == "blogger":
            file_url = "https://drive.google.com/uc?" + urlencode(
                {"export": "download", "id": f"{maker}_{number}"})
        http.files[file_url] = Response(
            subtitle(number), file_url,
            {"Content-Type": "application/x-subrip",
             "Content-Disposition": f'attachment; filename="{number:02d}.srt"'})
    for term in search_terms or [title]:
        if provider == "blogger":
            http.json[search(term)] = {"feed": {
                "openSearch$totalResults": {"$t": str(len(entries))}, "entry": entries}}
        else:
            http.pages[search(term)] = "".join(links)
    return {"name": maker, "episode": 3, "website": page(3)}


def fixture(provider, *, title=SHORT, source_title=None, official=LONG,
            search_terms=None, distractors=False):
    http = FakeHttp()
    creator = add_blog(http, provider, title=title, source_title=source_title,
                       search_terms=search_terms, distractors=distractors)
    http.json[CAPTIONS] = {"data": [creator]}
    return http, Anime(4200, official)


class ShortBlogTitleTests(unittest.TestCase):
    def test_registered_short_title_finds_each_episode_and_browse_all_on_each_blog(self):
        for provider in ("naver", "tistory", "blogger"):
            for episode in (1, 2, 3, None):
                with self.subTest(provider=provider, episode=episode):
                    http, anime = fixture(provider, distractors=True)
                    result = ReAnime(http).search_selected(
                        Query(LONG, season=1, episode=episode), anime)
                    expected = {1, 2, 3} if episode is None else {episode}
                    self.assertEqual({item.episode for item in result.candidates}, expected)
                    self.assertEqual(len(result.candidates), len(expected))
                    self.assertEqual({item.creator for item in result.candidates}, {"maker"})
                    self.assertTrue(all(SHORT in item.title for item in result.candidates))
                    self.assertFalse(result.warnings)

    def test_short_title_candidates_download_requested_and_browsed_episode(self):
        for provider in ("naver", "tistory", "blogger"):
            for episode in (2, None):
                with self.subTest(provider=provider, episode=episode):
                    http, anime = fixture(provider)
                    query = Query(LONG, episode=episode)
                    result = ReAnime(http).search_selected(query, anime)
                    candidate = next(item for item in result.candidates if item.episode == 2)
                    with tempfile.TemporaryDirectory() as folder:
                        target, note = Downloader(Path(folder), http).download(candidate, query)
                        self.assertEqual(target.read_bytes(), subtitle(2))
                        self.assertEqual(note, "")
                        metadata = json.loads(target.with_name(
                            target.name + ".source.json").read_text(encoding="utf-8"))
                        self.assertEqual(metadata["episode"], 2)
                        self.assertEqual(metadata["title"], LONG)
                        self.assertEqual(metadata["source_url"], candidate.source_url)

    def test_source_confirms_short_title_without_subtitle_delimiter(self):
        for provider in ("naver", "tistory", "blogger"):
            with self.subTest(provider=provider):
                http, anime = fixture(provider, official=EXTENDED,
                                      source_title=f"[자막] {SHORT} 03화 자막")
                result = ReAnime(http).search_selected(Query(EXTENDED, episode=2), anime)
                self.assertEqual([item.episode for item in result.candidates], [2])

    def test_unrelated_source_cannot_confirm_short_title(self):
        for source_title in ("관계없는 다른 작품 03화 자막",
                             "아가씨 돌 03화 자막",
                             "돌보기 03화 자막"):
            with self.subTest(source_title=source_title):
                http, anime = fixture("blogger", official=EXTENDED,
                                      source_title=source_title, search_terms=[EXTENDED, SHORT])
                result = ReAnime(http).search_selected(Query(EXTENDED, episode=2), anime)
                self.assertEqual(result.candidates, [])

    def test_confirmed_short_title_is_local_to_its_creator(self):
        http, anime = fixture("blogger", official=EXTENDED)
        second = add_blog(http, "blogger", maker="othermaker",
                          source_title="관계없는 다른 작품 03화 자막",
                          search_terms=[EXTENDED, SHORT])
        http.json[CAPTIONS]["data"].append(second)
        result = ReAnime(http).search_selected(Query(EXTENDED, episode=2), anime)
        self.assertEqual([item.creator for item in result.candidates], ["maker"])

    def test_source_shortening_preserves_sequel_and_special_markers(self):
        for marker in ("2기", "II", "Part 2", "Cour 2", "OVA", "OAD", "SP",
                       "특별편", "극장판"):
            official = f"{SHORT} {marker} 명문 학교 편"
            with self.subTest(marker=marker):
                http, anime = fixture("blogger", official=official,
                                      search_terms=[official, SHORT])
                result = ReAnime(http).search_selected(Query(official, episode=2), anime)
                self.assertEqual(result.candidates, [])

    def test_confirmed_short_title_can_retain_season_marker(self):
        short = f"{SHORT} 2기"
        official = f"{short} 명문 학교 편"
        http, anime = fixture("blogger", title=short, official=official)
        result = ReAnime(http).search_selected(Query(official, episode=2), anime)
        self.assertEqual([item.episode for item in result.candidates], [2])
        self.assertEqual(result.candidates[0].season, 2)

    def test_aggregate_short_title_remains_reviewable_and_downloads_requested_episode(self):
        source = "https://maker.blogspot.com/2026/09/aggregate.html"
        drive = "https://drive.google.com/file/d/all_episodes/view"
        http = FakeHttp()
        http.pages[source] = (f'<meta property="og:title" content="{SHORT}">'
                              f'<a href="{drive}">전체 자막</a>')
        http.json[CAPTIONS] = {"data": [
            {"name": "maker", "episode": 3, "website": source}]}
        feed_url = "https://maker.blogspot.com/feeds/posts/default?" + urlencode(
            {"alt": "json", "max-results": 100, "q": SHORT})
        http.json[feed_url] = {"feed": {"entry": []}}
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as target:
            for episode in (1, 2, 3):
                target.writestr(f"{episode:02d}.srt", subtitle(episode))
        file_url = "https://drive.google.com/uc?" + urlencode(
            {"export": "download", "id": "all_episodes"})
        http.files[file_url] = Response(archive.getvalue(), file_url,
            {"Content-Disposition": 'attachment; filename="all_episodes.zip"'})
        for episode in (2, None):
            with self.subTest(episode=episode):
                query = Query(LONG, episode=episode)
                result = ReAnime(http).search_selected(query, Anime(4200, LONG))
                self.assertEqual(result.status, "review")
                self.assertFalse(result.warnings)
                self.assertEqual(len(result.candidates), 1)
                self.assertEqual(result.candidates[0].source_url, source)
                self.assertEqual(result.candidates[0].episode, episode)
                self.assertTrue(result.candidates[0].require_episode)
                if episode is not None:
                    with tempfile.TemporaryDirectory() as folder:
                        path, note = Downloader(Path(folder), http).download(
                            result.candidates[0], query)
                        self.assertEqual(path.read_bytes(), subtitle(episode))
                        self.assertEqual(note, "")
        searched_titles = [parse_qs(urlsplit(url).query)["q"][0] for url in http.calls
                           if urlsplit(url).path == "/feeds/posts/default"]
        self.assertEqual(searched_titles, [SHORT])


if __name__ == "__main__":
    unittest.main()
