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
    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.calls: list[tuple[str, dict, dict]] = []

    def post_form(self, url, form, *, headers=None, limit=0):
        self.calls.append((url, form.copy(), (headers or {}).copy()))
        return Response(self.pages[int(form["pagenum"])].encode("euc-kr"), url,
                        {"Content-Type": "text/html; charset=euc-kr"})


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

    def test_only_exact_bigfile_host_is_allowed(self):
        self.assertTrue(allowed_url(Bigfile.SEARCH))
        self.assertFalse(allowed_url("https://fake.bigfile.co.kr/ajax/getContentList.php"))


if __name__ == "__main__":
    unittest.main()
