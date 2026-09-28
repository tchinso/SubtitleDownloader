"""Browse-all searches must not stop at a creator's newest post."""

import unittest

from subfinder.models import Query
from subfinder.network import SourceError
from subfinder.providers.reanime import ReAnime


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url):
        try:
            return self.pages[url]
        except KeyError as exc:
            raise SourceError(f"Missing fixture: {url}") from exc


def naver_post(number):
    return (f'<meta property="og:title" content="Bookworm {number}화 자막">'
            f'<a href="https://download.blog.naver.com/{number}.srt" '
            f'download="{number}.srt">자막</a>')


def tistory_post(number):
    return (f'<meta property="og:title" content="Bookworm {number}화 자막">'
            f'<a href="https://blog.kakaocdn.net/file/{number}.srt">자막</a>')


class BrowseAllSourceTests(unittest.TestCase):
    def test_naver_collects_latest_and_older_search_pages(self):
        base = "https://blog.naver.com/PostSearchList.naver?blogId=maker&SearchText=Bookworm"
        pages = {f"https://blog.naver.com/maker/{number}": naver_post(number)
                 for number in (20, 21, 22, 23)}
        pages[base] = (
            '<a href="/maker/23">Bookworm 23화 자막</a>'
            '<a href="/maker/22">Bookworm 22화 자막</a>'
            '<a href="/maker/21">Bookworm 21화 자막</a>'
            '<a href="/PostSearchList.naver?blogId=maker&amp;SearchText=Bookworm&amp;page=2">이전</a>')
        pages[base + "&page=2"] = '<a href="/maker/20">Bookworm 20화 자막</a>'

        found = ReAnime(FakeHttp(pages))._naver(
            "https://blog.naver.com/maker/23", ["Bookworm"], Query("Bookworm"),
            "Maker", ["Bookworm"])

        self.assertEqual({item.episode for item in found}, {20, 21, 22, 23})
        self.assertEqual(len(found), 4)

    def test_tistory_collects_latest_and_older_search_pages(self):
        base = "https://maker.tistory.com/search/Bookworm"
        pages = {f"https://maker.tistory.com/{number}": tistory_post(number)
                 for number in (20, 21, 22, 23)}
        pages[base] = (
            '<a href="/23">Bookworm 23화 자막</a>'
            '<a href="/22">Bookworm 22화 자막</a>'
            '<a href="/21">Bookworm 21화 자막</a>'
            '<a href="/search/Bookworm?page=2">이전</a>')
        pages[base + "?page=2"] = '<a href="/20">Bookworm 20화 자막</a>'

        found = ReAnime(FakeHttp(pages))._tistory(
            "https://maker.tistory.com/23", ["Bookworm"], Query("Bookworm"),
            "Maker", ["Bookworm"])

        self.assertEqual({item.episode for item in found}, {20, 21, 22, 23})
        self.assertEqual(len(found), 4)


if __name__ == "__main__":
    unittest.main()
