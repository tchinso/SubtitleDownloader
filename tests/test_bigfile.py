import unittest

from subfinder.models import Query
from subfinder.network import Response, allowed_url
from subfinder.providers.bigfile import Bigfile


def page(rows: list[tuple[str, str, str]], last: int = 1) -> str:
    listing = "<table>" + "".join(
        '<tr><td>[애니]</td><td>&nbsp;' + filename + '</td>'
        '<td><a onclick="downLoad(\'' + content_id + "','" + caption_id + "')\">다운로드</a></td></tr>"
        for filename, content_id, caption_id in rows
    ) + "</table>"
    pagination = (
        '<a href="javascript:CallCommentList(\'&pageCount=20&cateGory=0005\','
        + str(last) + ');" class="last">마지막페이지</a>'
    )
    return listing + "^^^^^^^" + pagination


class FakeHttp:
    def __init__(self, pages: dict[int | tuple[str, int], str], aliases=None):
        self.pages = pages
        self.calls: list[tuple[str, dict, dict]] = []
        self.aliases = aliases or []
        self.alias_calls = 0

    def post_form(self, url, form, *, headers=None, limit=0):
        self.calls.append((url, form.copy(), (headers or {}).copy()))
        key = (form["searchCaption"], int(form["pagenum"]))
        html = self.pages.get(key, self.pages.get(key[1], page([])))
        return Response(html.encode("euc-kr"), url,
                        {"Content-Type": "text/html; charset=euc-kr"})

    def get_json(self, url, *, method="GET", payload=None, headers=None):
        self.alias_calls += 1
        return {"data": {"Page": {"media": self.aliases}}}


class BigfileTests(unittest.TestCase):
    def test_anime_category_search_reaches_later_episode_page(self):
        http = FakeHttp({
            1: page([("Example S01E23.srt", "101", "201")], last=2),
            2: page([("Example S01E22.smi", "102", "202")], last=2),
        })
        result = Bigfile(http).search(Query("Example", season=1, episode=22))

        self.assertEqual(result.status, "found")
        self.assertEqual([c.file_name for c in result.candidates], ["Example S01E22.smi"])
        self.assertEqual(result.candidates[0].confidence, "exact")
        self.assertEqual(result.candidates[0].season, 1)
        self.assertEqual(result.candidates[0].episode, 22)
        self.assertEqual(result.candidates[0].download_url, "")
        self.assertEqual(result.candidates[0].source_url, Bigfile.PAGE)
        self.assertIn("Example", result.candidates[0].note)
        self.assertTrue(all(call[1]["cateGory"] == "0005" for call in http.calls))
        self.assertEqual([call[1]["pagenum"] for call in http.calls], ["1", "2"])
        self.assertEqual(http.alias_calls, 0)

    def test_listing_ignores_other_categories_and_non_caption_rows(self):
        html = page([("Example 02.srt", "101", "201")])
        html = html.replace("[애니]", "[영화]")
        html = html.replace("</table>",
            '<tr><td>[애니]</td><td>Example 02.exe</td>'
            '<td><a onclick="downLoad(\'103\',\'203\')">다운로드</a></td></tr></table>', 1)
        result = Bigfile(FakeHttp({1: html})).search(Query("Example"))
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.candidates, [])

    def test_rejects_korean_only_query_and_unreadable_response(self):
        http = FakeHttp({1: "<html>Login</html>"})
        source = Bigfile(http)
        self.assertEqual(source.search(Query("예시 작품")).status, "error")
        self.assertEqual(http.calls, [])
        result = source.search(Query("Example"))
        self.assertEqual(result.status, "error")
        self.assertTrue(any("응답 형식" in warning for warning in result.warnings))
        self.assertEqual(http.alias_calls, 0)

    def test_only_exact_bigfile_host_is_allowed(self):
        self.assertTrue(allowed_url(Bigfile.SEARCH))
        self.assertFalse(allowed_url("https://fake.bigfile.co.kr/ajax/getContentList.php"))

    def test_official_english_name_falls_back_to_exact_romaji_title(self):
        english = "Ascendance of a Bookworm"
        romaji = "Honzuki no Gekokujou: Shisho ni Naru Tame ni wa Shudan wo Erandeiraremasen"
        short = "Honzuki no Gekokujou"
        http = FakeHttp({
            (english, 1): page([]),
            (romaji, 1): page([]),
            (short, 1): page([("Honzuki no Gekokujou S4 - 22.smi", "401", "501")]),
        }, aliases=[{"title": {"english": english, "romaji": romaji, "native": "本好きの下剋上"},
                     "synonyms": []}])

        result = Bigfile(http).search(Query(english, episode=22))

        self.assertEqual(result.status, "found")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].title, english)
        self.assertEqual(result.candidates[0].episode, 22)
        self.assertIn(short, result.candidates[0].note)
        self.assertEqual([call[1]["searchCaption"] for call in http.calls],
                         [english, romaji, short])
        self.assertEqual(http.alias_calls, 1)

    def test_ambiguous_alias_does_not_search_unrelated_romaji(self):
        english = "Example"
        http = FakeHttp({1: page([])}, aliases=[
            {"title": {"english": english, "romaji": "First"}, "synonyms": []},
            {"title": {"english": english, "romaji": "Second"}, "synonyms": []},
        ])
        result = Bigfile(http).search(Query(english))
        self.assertEqual(result.status, "empty")
        self.assertEqual(len(http.calls), 1)


if __name__ == "__main__":
    unittest.main()
