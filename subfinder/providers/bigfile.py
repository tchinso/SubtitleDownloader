"""Search Bigfile's public anime subtitle listing.

The listing is public, but Bigfile requires a user login to download files.
Results therefore point users to the site for the actual download.
"""

from html.parser import HTMLParser
from pathlib import PurePosixPath
import re

from ..matching import ARCHIVES, FORMATS, filename_episode, normal, season_of
from ..models import Candidate, Query, SearchResult
from ..network import HttpClient, SourceError


class _CaptionRows(HTMLParser):
    """Read only caption table rows, never execute the site's onclick code."""

    _download = re.compile(r"\s*downLoad\(\s*['\"](\d+)['\"]\s*,\s*['\"](\d+)['\"]\s*\)\s*;?\s*")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str, str]] = []
        self._cells: list[str] | None = None
        self._cell: list[str] | None = None
        self._ids: tuple[str, str] | None = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "tr":
            self._cells = []
            self._ids = None
        elif tag == "td" and self._cells is not None:
            self._cell = []
        elif tag == "a" and self._cells is not None:
            match = self._download.fullmatch(attrs.get("onclick", ""))
            if match:
                self._ids = match.groups()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._cells is not None and self._cell is not None:
            self._cells.append("".join(self._cell).replace("\xa0", " ").strip())
            self._cell = None
        elif tag == "tr" and self._cells is not None:
            if len(self._cells) >= 3 and self._ids and "애니" in self._cells[0]:
                name = self._cells[1]
                if PurePosixPath(name.replace("\\", "/")).suffix.lower() in FORMATS | ARCHIVES:
                    self.rows.append((name, *self._ids))
            self._cells = None
            self._cell = None
            self._ids = None


def _last_page(html: str) -> int:
    pages = [int(value) for value in re.findall(r"CallCommentList\([^)]*,\s*(\d+)\s*\)", html)]
    return max([1, *pages])


class Bigfile:
    PAGE = "https://www.bigfile.co.kr/content/freecaption.php?cateGory=0005"
    SEARCH = "https://www.bigfile.co.kr/ajax/getContentList.php"
    MAX_PAGES = 50

    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()

    def search(self, query: Query) -> SearchResult:
        term = query.title.strip()
        if not re.search(r"[a-z]", term, re.I):
            return SearchResult(status="error", warnings=["빅파일 애니 검색에는 영문 작품명을 입력해 줘"])

        result = self._search_term(query, term)
        if result.status != "empty":
            return result

        # Bigfile indexes filenames, which commonly use the Japanese romaji
        # title even when a user enters the official English title.
        aliases, alias_warnings = self._romaji_aliases(term)
        for alias in aliases:
            alternative = self._search_term(query, alias)
            if alternative.status == "found":
                alternative.warnings.insert(0, f"빅파일에 '{term}' 결과가 없어 '{alias}'로 검색했어")
                alternative.warnings.extend(alias_warnings)
                return alternative
            if alternative.status == "error":
                alternative.warnings.extend(alias_warnings)
                return alternative
        result.warnings.extend(alias_warnings)
        return result

    def _romaji_aliases(self, term: str) -> tuple[list[str], list[str]]:
        gql = ('query ($search: String!) { Page(perPage: 10) {'
               ' media(search: $search, type: ANIME) { title { english romaji native } synonyms } } }')
        try:
            data = self.http.get_json("https://graphql.anilist.co", method="POST",
                                      payload={"query": gql, "variables": {"search": term}})
            media = ((data.get("data") or {}).get("Page") or {}).get("media") or []
            exact = []
            for entry in media:
                title = entry.get("title") or {}
                values = list(title.values()) + (entry.get("synonyms") or [])
                if any(isinstance(value, str) and normal(value) == normal(term) for value in values):
                    exact.append(title.get("romaji"))
            if len(exact) != 1 or not isinstance(exact[0], str):
                return [], []
            romaji = exact[0].strip()
            variants = [romaji, romaji.split(":", 1)[0].strip()]
            return list(dict.fromkeys(value for value in variants
                                      if value and re.search(r"[a-z]", value, re.I)
                                      and normal(value) != normal(term))), []
        except (SourceError, AttributeError, TypeError, ValueError) as exc:
            return [], [f"AniList 영문명 별칭 확인: {exc}"]

    def _search_term(self, query: Query, term: str) -> SearchResult:
        found: list[Candidate] = []
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        page = 1
        last = 1
        while page <= min(last, self.MAX_PAGES):
            try:
                response = self.http.post_form(
                    self.SEARCH,
                    {"pagenum": str(page), "cateGory": "0005", "searchCaption": term},
                    headers={"Referer": self.PAGE},
                )
                html = response.body.decode("euc-kr", errors="replace")
                if "^^^^^^^" not in html:
                    raise SourceError("빅파일 검색 응답 형식을 읽지 못함")
                listing, pagination = html.split("^^^^^^^", 1)
                parsed = _CaptionRows()
                parsed.feed(listing)
                if page == 1:
                    last = _last_page(pagination)
                new_ids = 0
                for filename, content_id, caption_id in parsed.rows:
                    ident = (content_id, caption_id)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    new_ids += 1
                    episode = filename_episode(filename)
                    season = season_of(filename)
                    if query.episode is not None and episode is not None and episode != query.episode:
                        continue
                    if query.season is not None and season is not None and season != query.season:
                        continue
                    confidence = "exact" if query.episode is not None and episode == query.episode else "review"
                    found.append(Candidate(
                        provider="Bigfile", title=query.title, file_name=filename,
                        source_url=self.PAGE, language="ko", season=season,
                        episode=episode if isinstance(episode, int) else None,
                        confidence=confidence,
                        note=f"빅파일에서 '{term}' 검색 · 로그인 후 사이트에서 '{filename}' 다운로드",
                    ))
                if page > 1 and parsed.rows and not new_ids:
                    warnings.append("빅파일 페이지가 반복되어 검색을 중단했어")
                    break
            except (SourceError, UnicodeError, ValueError) as exc:
                warnings.append(f"빅파일 애니 검색 {page}페이지: {exc}")
                return SearchResult(found, warnings, "found" if found else "error")
            page += 1

        if last > self.MAX_PAGES:
            warnings.append(f"빅파일 결과가 많아 처음 {self.MAX_PAGES}페이지만 확인했어")
        return SearchResult(found, warnings, "found" if found else "empty")
