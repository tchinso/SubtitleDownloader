import json
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from urllib.request import Request, urlopen
from unittest.mock import Mock
import zipfile
from io import BytesIO

from subfinder.bridge import BridgeServer
from subfinder.downloader import Downloader, safe_name
from subfinder.engine import SearchEngine
from subfinder.models import Anime, Candidate, Query, SearchResult
from subfinder.network import Response, SourceError, allowed_url
from subfinder.providers.opensubtitles import OpenSubtitles
from subfinder.providers.reanime import ReAnime


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.json = {}
        self.html = {}
        self.binary = {}

    def get_json(self, url, **options):
        self.calls.append(("json", url, options))
        if url not in self.json:
            raise SourceError("fixture not found: " + url)
        return self.json[url]

    def get_text(self, url):
        self.calls.append(("text", url, {}))
        if url not in self.html:
            raise SourceError("fixture not found: " + url)
        return self.html[url]

    def get_bytes(self, url, limit=6 * 1024 * 1024):
        self.calls.append(("bytes", url, {}))
        if url not in self.binary:
            raise SourceError("fixture not found: " + url)
        return self.binary[url]


def zip_bytes(files):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return stream.getvalue()


class FlowTests(unittest.TestCase):
    def test_reanime_first_does_not_query_open_when_candidate_exists(self):
        first = Mock(search=Mock(return_value=SearchResult([Candidate("ReAnime", "Example", "01.srt", "https://blog.naver.com/a/1", "https://download.blog.naver.com/01.srt")], status="found")))
        second = Mock(search=Mock())
        result = SearchEngine(first, second).search(Query("Example", episode=1))
        self.assertEqual(len(result.candidates), 1)
        second.search.assert_not_called()

    def test_missing_reanime_automatically_searches_open(self):
        first = Mock(search=Mock(return_value=SearchResult(status="empty")))
        second = Mock(search=Mock(return_value=SearchResult([Candidate("OpenSubtitles", "Example", "01.srt", "https://www.opensubtitles.com", file_id=55)], status="found")))
        result = SearchEngine(first, second).search(Query("Example", episode=1))
        self.assertTrue(result.fallback_used)
        second.search.assert_called_once()

    def test_selected_anime_empty_search_falls_back_with_selected_title(self):
        anime = Anime(12, "외톨이의 이세계 공략")
        first = Mock(search_selected=Mock(return_value=SearchResult(status="empty")))
        second = Mock(search=Mock(return_value=SearchResult(status="empty")))
        query = Query(anime.subject, episode=1)

        result = SearchEngine(first, second).search_selected(query, anime)

        first.search_selected.assert_called_once_with(query, anime)
        second.search.assert_called_once_with(query)
        self.assertTrue(result.fallback_used)

    def test_error_is_not_misreported_as_absent_and_manual_search_works(self):
        first = Mock(search=Mock(return_value=SearchResult(status="error", warnings=["HTTP 403"])))
        second = Mock(search=Mock(return_value=SearchResult(status="empty")))
        engine = SearchEngine(first, second)
        result = engine.search(Query("Example"))
        self.assertEqual(result.status, "error")
        second.search.assert_not_called()
        combined = engine.search_open(Query("Example"), result)
        second.search.assert_called_once()
        self.assertIsNot(combined, result)
        self.assertEqual(result.warnings, ["HTTP 403"])

    def test_other_languages_skip_korean_creator_search(self):
        first, second = Mock(search=Mock()), Mock(search=Mock(return_value=SearchResult(status="empty")))
        SearchEngine(first, second).search(Query("Example", language="ja"))
        first.search.assert_not_called()
        second.search.assert_called_once()


class SourceTests(unittest.TestCase):
    def test_keyword_discovery_accepts_zero_pages_for_no_matches(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=missing"] = {
            "code": "ok", "data": {"totalPages": 0, "content": []}}

        result = ReAnime(http).discover("missing")

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.anime, [])
        self.assertEqual(result.warnings, [])

    def test_keyword_discovery_reads_all_anissia_pages_without_exact_title_filter(self):
        http = FakeHttp()
        base = "https://api.anissia.net/anime/list/"
        query = "?q=%EC%9D%B4%EC%84%B8%EA%B3%84"
        http.json[base + "0" + query] = {"code": "ok", "data": {"totalPages": 3, "content": [
            {"animeNo": 1, "subject": "외톨이의 이세계 공략", "originalSubject": ""},
            {"animeNo": 2, "subject": "관계없는 작품", "originalSubject": "이세계의 여행"}]}}
        http.json[base + "1" + query] = {"code": "ok", "data": {"totalPages": 3, "content": [
            {"animeNo": 1, "subject": "외톨이의 이세계 공략"},
            {"animeNo": 3, "subject": "이세계 유유자적 농가"}]}}
        http.json[base + "2" + query] = {"code": "ok", "data": {"totalPages": 3, "content": [
            {"animeNo": 4, "subject": "이세계 콰르텟"},
            {"animeNo": 5, "subject": "다른 세계"}]}}

        result = ReAnime(http).discover("이세계")

        self.assertEqual(result.status, "found")
        self.assertEqual([anime.anime_no for anime in result.anime], [1, 2, 3, 4])
        self.assertEqual(result.anime[0].subject, "외톨이의 이세계 공략")
        self.assertEqual([url for kind, url, _ in http.calls if kind == "json"],
                         [base + str(page) + query for page in range(3)])

    def test_keyword_discovery_keeps_partial_results_and_reports_page_failure(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=%EC%9D%B4%EC%84%B8%EA%B3%84"] = {
            "data": {"totalPages": 2, "content": [{"animeNo": 12, "subject": "이세계 공략"}]}}

        result = ReAnime(http).discover("이세계")

        self.assertEqual(result.status, "found")
        self.assertEqual(result.anime, [Anime(12, "이세계 공략")])
        self.assertTrue(any("2페이지" in warning for warning in result.warnings))

    def test_selected_anime_uses_its_id_and_title_without_reidentification(self):
        http = FakeHttp()
        captions = "https://api.anissia.net/anime/caption/animeNo/12"
        http.json[captions] = {"data": [{"name": "Maker", "website": "https://blog.naver.com/maker/123"}]}
        http.html["https://blog.naver.com/maker/123"] = (
            '<meta property="og:title" content="외톨이의 이세계 공략 01화 자막">'
            '<a href="https://download.blog.naver.com/open/01.srt" download="01.srt">file</a>')

        result = ReAnime(http).search_selected(Query("이세계", episode=1), Anime(12, "외톨이의 이세계 공략"))

        self.assertEqual(result.status, "found")
        self.assertEqual(result.candidates[0].title, "외톨이의 이세계 공략 01화 자막")
        self.assertEqual([url for kind, url, _ in http.calls if kind == "json"], [captions])

    def test_optional_title_lookup_failure_does_not_block_open_fallback(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=Example"] = {"data": {"content": []}}
        http.json["https://api.anissia.net/anime/list/0"] = {"data": {"content": [], "totalPages": 1}}
        http.html["https://www.google.com/search?hl=ko&q=Example+%EC%9E%90%EB%A7%89"] = "<html>No matches</html>"
        open_source = Mock(search=Mock(return_value=SearchResult(status="empty")))
        result = SearchEngine(ReAnime(http), open_source).search(Query("Example"))
        self.assertTrue(result.fallback_used)
        open_source.search.assert_called_once()
        self.assertTrue(any("AniList" in warning for warning in result.warnings))

    def test_entire_google_fallback_search_and_download_work_without_edge(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=Example"] = {"data": {"content": []}}
        http.json["https://api.anissia.net/anime/list/0"] = {"data": {"content": [], "totalPages": 1}}
        google = "https://www.google.com/search?hl=ko&q=Example+1%ED%99%94+%EC%9E%90%EB%A7%89"
        http.html[google] = '<a href="https://maker.tistory.com/123"><h3>Example 01화 자막</h3></a>'
        http.html["https://maker.tistory.com/123"] = '<meta property="og:title" content="Example 01화 자막"><a href="https://blog.kakaocdn.net/file/01.srt">자막</a>'
        http.binary["https://blog.kakaocdn.net/file/01.srt"] = Response(b"1\n00:00:01,000 --> 00:00:02,000\nHello", "https://blog.kakaocdn.net/file/01.srt", {})
        other = Mock(search=Mock())
        result = SearchEngine(ReAnime(http), other).search(Query("Example", season=1, episode=1))
        self.assertEqual(len(result.candidates), 1)
        other.search.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            target, note = Downloader(Path(folder) / "subtitles", http).download(result.candidates[0], Query("Example", season=1, episode=1))
            self.assertIn(b"Hello", target.read_bytes())
            self.assertEqual(note, "")

    def test_google_continues_when_first_result_has_no_subtitle_file(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=%EC%98%88%EC%8B%9C"] = {"data": {"content": []}}
        http.json["https://api.anissia.net/anime/list/0"] = {"data": {"content": [], "totalPages": 1}}
        http.html["https://www.google.com/search?hl=ko&q=%EC%98%88%EC%8B%9C+1%ED%99%94+%EC%9E%90%EB%A7%89"] = '<a href="https://maker.tistory.com/123"><h3>예시 1화 자막</h3></a>'
        http.html["https://maker.tistory.com/123"] = '<meta property="og:title" content="예시 1화 자막">'
        http.html["https://maker.tistory.com/search/%EC%98%88%EC%8B%9C"] = '<html>No matching posts</html>'
        http.html["https://www.google.com/search?hl=ko&q=%EC%98%88%EC%8B%9C+%EC%9E%90%EB%A7%89"] = '<a href="https://maker2.tistory.com/456"><h3>예시 1화 자막</h3></a>'
        http.html["https://maker2.tistory.com/456"] = '<meta property="og:title" content="예시 1화 자막"><a href="https://blog.kakaocdn.net/file/01.srt">자막</a>'
        result = ReAnime(http).search(Query("예시", episode=1))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].source_url, "https://maker2.tistory.com/456")

    def test_manual_title_reads_anissia_and_naver_attachment_without_reanime_site(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=Example"] = {"data": {"content": [{"animeNo": 12, "subject": "Example"}]}}
        http.json["https://api.anissia.net/anime/caption/animeNo/12"] = {"data": [{"name": "Maker", "website": "https://blog.naver.com/maker/123"}]}
        http.html["https://blog.naver.com/maker/123"] = '<meta property="og:title" content="Example 01화 자막"><a href="https://download.blog.naver.com/open/01.srt" download="Example 01화.srt">file</a>'
        result = ReAnime(http).search(Query("Example", episode=1))
        self.assertEqual(result.status, "found")
        self.assertEqual(result.candidates[0].provider, "ReAnime/네이버")
        self.assertFalse(any("reanime.to" in url for _, url, _ in http.calls))

    def test_deleted_naver_latest_post_searches_same_creator_and_upgrades_http(self):
        http = FakeHttp()
        http.json["https://api.anissia.net/anime/list/0?q=%EC%98%88%EC%8B%9C"] = {"data": {"content": [{"animeNo": 12, "subject": "예시"}]}}
        http.json["https://api.anissia.net/anime/caption/animeNo/12"] = {"data": [{"name": "Maker", "website": "http://blog.naver.com/maker/123"}]}
        http.html["https://blog.naver.com/PostSearchList.naver?blogId=maker&SearchText=%EC%98%88%EC%8B%9C"] = '<a href="/maker/456">예시 1화 자막</a>'
        http.html["https://blog.naver.com/maker/456"] = '<meta property="og:title" content="예시 1화 자막"><a href="https://download.blog.naver.com/01.srt" download="01.srt">file</a>'
        result = ReAnime(http).search(Query("예시", episode=1))
        self.assertEqual(result.status, "found")
        self.assertEqual(result.candidates[0].source_url, "https://blog.naver.com/maker/456")

    def test_tistory_search_finds_kakao_attachment(self):
        http = FakeHttp()
        http.html["https://maker.tistory.com/search/Example"] = '<a href="/123">Example 02화 자막</a>'
        http.html["https://maker.tistory.com/123"] = '<meta property="og:title" content="Example 02화 자막"><a href="https://blog.kakaocdn.net/file/Example02.srt">file</a>'
        result = ReAnime(http).direct("https://maker.tistory.com/123", Query("Example", episode=2))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].download_url, "https://blog.kakaocdn.net/file/Example02.srt")

    def test_blogger_feed_finds_drive_link(self):
        http = FakeHttp()
        http.json["https://maker.blogspot.com/feeds/posts/default?alt=json&max-results=100&q=Example"] = {"feed": {"entry": [{"title": {"$t": "Example 03화 자막"}, "content": {"$t": '<a href="https://drive.google.com/file/d/file_123/view">3화</a>'}, "link": [{"rel": "alternate", "href": "https://maker.blogspot.com/2026/09/post.html"}]}]}}
        result = ReAnime(http)._blogger("https://maker.blogspot.com", ["Example"], Query("Example", episode=3), "Maker")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].download_url, "https://drive.google.com/file/d/file_123/view")

    def test_open_api_search_download_and_origin_validation(self):
        http = FakeHttp()
        source = OpenSubtitles(http, "test-key")
        url = source.BASE + "/subtitles?query=Example&languages=ko&type=episode&season_number=1&episode_number=2&page=1"
        http.json[url] = {"data": [{"attributes": {"language": "ko", "feature_details": {"title": "Example", "season_number": 1, "episode_number": 2}, "files": [{"file_id": 77, "file_name": "Example.S01E02.srt"}]}}], "total_pages": 1}
        result = source.search(Query("Example", season=1, episode=2))
        self.assertEqual(result.candidates[0].file_id, 77)
        self.assertEqual(result.candidates[0].confidence, "exact")
        http.json[source.BASE + "/download"] = {"link": "https://www.opensubtitles.com/subtitle/77", "file_name": "Example.srt"}
        http.binary["https://www.opensubtitles.com/subtitle/77"] = Response(b"1\n00:00:01,000 --> 00:00:02,000\nHi", "https://www.opensubtitles.com/subtitle/77", {})
        body, name = source.download(result.candidates[0])
        self.assertEqual(name, "Example.srt")
        self.assertIn(b"Hi", body)
        self.assertEqual(next(options["headers"]["Api-Key"] for kind, address, options in http.calls if address == url), "test-key")
        http.json[source.BASE + "/download"]["link"] = "http://127.0.0.1:9999/secret"
        with self.assertRaises(SourceError):
            source.download(result.candidates[0])


class DownloadTests(unittest.TestCase):
    def test_zip_selects_requested_episode_and_writes_source_metadata(self):
        http = FakeHttp()
        link = "https://blog.kakaocdn.net/download/all.zip"
        http.binary[link] = Response(zip_bytes({"../S01E01.srt": "first", "folder/S01E02.srt": "second"}), link, {})
        with tempfile.TemporaryDirectory() as folder:
            target, note = Downloader(Path(folder) / "subtitles", http).download(
                Candidate("ReAnime/티스토리", "Example", "all.zip", "https://maker.tistory.com/123", link),
                Query("Example", season=1, episode=2))
            self.assertEqual(target.read_bytes(), b"second")
            self.assertEqual(target.parent.name, "Season 01")
            self.assertTrue(target.with_name(target.name + ".source.json").exists())
            self.assertNotIn("..", str(target))
            self.assertEqual(note, "")

    def test_ambiguous_archive_preserved_and_windows_names_sanitized(self):
        http = FakeHttp()
        link = "https://blog.kakaocdn.net/all.zip"
        original = zip_bytes({"a/S01E02.srt": "first", "b/S01E02.srt": "second"})
        http.binary[link] = Response(original, link, {})
        with tempfile.TemporaryDirectory() as folder:
            target, note = Downloader(Path(folder) / "subtitles", http).download(
                Candidate("ReAnime", "Example", "all.zip", link, link), Query('CON:<>', season=1, episode=2))
            self.assertEqual(target.read_bytes(), original)
            self.assertIn("원본 ZIP", note)
            self.assertTrue(target.name.endswith(".zip"))
        self.assertTrue(safe_name("CON").startswith("_"))
        self.assertEqual(safe_name("../evil?.srt"), "_evil_.srt")

    def test_rejects_non_https_and_private_hosts(self):
        for url in ("http://example.com/01.srt", "https://127.0.0.1/x", "https://evil.com/x", "https://user:secret@blog.naver.com/x"):
            self.assertFalse(allowed_url(url))


class BridgeTests(unittest.TestCase):
    def test_local_edge_handoff_requires_token_and_extension_origin(self):
        inbox = Queue()
        bridge = BridgeServer("good-token", inbox, port=0)
        bridge.start()
        port = bridge.server.server_port
        bridge.port = port
        base = f"http://127.0.0.1:{port}"
        try:
            with self.assertRaises(Exception):
                urlopen(Request(base + "/v1/status", headers={"Authorization": "Bearer wrong"}), timeout=2)
            body = json.dumps({"title": "Example", "url": "https://example.org/anime"}).encode()
            headers = {"Authorization": "Bearer good-token", "Content-Type": "application/json",
                       "Origin": "chrome-extension://" + "a" * 32}
            preflight = Request(base + "/v1/send", method="OPTIONS",
                                headers={"Origin": headers["Origin"], "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "authorization,content-type"})
            with urlopen(preflight, timeout=2) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], headers["Origin"])
            with urlopen(Request(base + "/v1/send", data=body, headers=headers, method="POST"), timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], headers["Origin"])
            self.assertEqual(inbox.get_nowait()["title"], "Example")
            headers["Origin"] = "https://malicious.example"
            with self.assertRaises(Exception):
                urlopen(Request(base + "/v1/send", data=body, headers=headers, method="POST"), timeout=2)
            self.assertTrue(inbox.empty())
        finally:
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
