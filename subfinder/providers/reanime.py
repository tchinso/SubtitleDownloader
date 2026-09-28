"""Anissia + creator blogs, followed by Google public-result discovery.

The adapter extracts public attachment links. It does not contain subtitle
files, reuse the ReAnime extension, or execute scripts from source pages.
"""

import json
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

from ..htmlparse import Document
from ..matching import (FORMATS, ARCHIVES, drive_id, episode_of, file_name_from_url,
                        matching_post, normal, season_of, suitable_file)
from ..models import Anime, AnimeSearchResult, Candidate, Query, SearchResult
from ..network import HttpClient, SourceError, allowed_url


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _naver_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname not in ("blog.naver.com", "m.blog.naver.com"):
        return None
    parts = parsed.path.strip("/").split("/")
    params = parse_qs(parsed.query)
    blog = params.get("blogId", [parts[0] if parts else ""])[0]
    post = params.get("logNo", [parts[1] if len(parts) > 1 else ""])[0]
    if re.fullmatch(r"[\w-]+", blog) and post.isdecimal():
        return f"https://blog.naver.com/{blog}/{post}"
    return None


def _decode_js_string(value: str) -> str:
    replacements = {"n": "\n", "r": "\r", "t": "\t", "'": "'", '"': '"', "\\": "\\", "/": "/"}
    def replace(match):
        item = match.group(1)
        if item[:1] in ("u", "x"):
            return chr(int(item[1:], 16))
        return replacements.get(item, item)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[nrt'\"\\/])", replace, value)


class ReAnime:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()
        self._catalog: list[dict] | None = None

    def discover(self, keyword: str) -> AnimeSearchResult:
        """Browse Anissia titles by keyword before searching a selected title's subtitles."""
        term = keyword.strip()
        wanted = normal(term)
        if not wanted:
            return AnimeSearchResult(status="error", warnings=["검색어를 입력해 줘"])

        anime: dict[int, Anime] = {}
        warnings: list[str] = []
        page = 0
        page_count = 1
        while page < page_count:
            url = f"https://api.anissia.net/anime/list/{page}?{urlencode({'q': term})}"
            try:
                response = self.http.get_json(url)
                if response.get("code", "ok") != "ok":
                    raise SourceError("애니시아 작품 검색 응답 오류")
                data = response.get("data") or {}
                content = data.get("content")
                pages = data.get("totalPages", 1)
                if (not isinstance(content, list) or not isinstance(pages, int)
                        or pages < 0 or (pages == 0 and (page != 0 or content))):
                    raise SourceError("애니시아 작품 검색 응답 형식 오류")
                if page == 0:
                    page_count = min(pages, 200)
                    if pages > 200:
                        warnings.append("애니시아 검색 결과는 처음 200페이지만 확인했어")
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    anime_no = item.get("animeNo")
                    subject = item.get("subject")
                    original = item.get("originalSubject") or ""
                    if (not isinstance(anime_no, int) or anime_no <= 0
                            or not isinstance(subject, str) or not subject.strip()
                            or not isinstance(original, str)):
                        continue
                    if wanted not in normal(subject) and wanted not in normal(original):
                        continue
                    anime.setdefault(anime_no, Anime(anime_no, subject, original))
            except (SourceError, AttributeError, TypeError, ValueError) as exc:
                warnings.append(f"애니시아 작품 검색 {page + 1}페이지: {exc}")
                break
            page += 1
        return AnimeSearchResult(list(anime.values()), warnings,
                                 "found" if anime else "error" if warnings else "empty")

    def names(self, query: Query, warnings: list[str]) -> list[str]:
        result = [query.title.strip()]
        if query.slug and re.fullmatch(r"[a-z0-9-]+", query.slug):
            try:
                data = self.http.get_json(f"https://reanime.to/api/v1/anime/{query.slug}")
                result.extend(t for t in (data.get("title") or {}).values() if isinstance(t, str))
            except (SourceError, AttributeError) as exc:
                warnings.append(f"ReAnime 작품 정보: {exc}")
        if re.search(r"[a-z]", query.title, re.I) and not re.search(r"[가-힣ぁ-んァ-ヶ一-龯]", query.title):
            gql = 'query ($search: String!) { Page(perPage: 10) { media(search: $search, type: ANIME) { title { english romaji native } synonyms } } }'
            try:
                data = self.http.get_json("https://graphql.anilist.co", method="POST",
                                          payload={"query": gql, "variables": {"search": query.title}})
                media = ((data.get("data") or {}).get("Page") or {}).get("media") or []
                exact = []
                for entry in media:
                    values = list((entry.get("title") or {}).values()) + (entry.get("synonyms") or [])
                    if any(isinstance(v, str) and normal(v) == normal(query.title) for v in values):
                        exact.append([v for v in values if isinstance(v, str)])
                if len(exact) == 1:
                    result.extend(exact[0])
            except (SourceError, AttributeError, TypeError) as exc:
                warnings.append(f"AniList 별칭: {exc}")
        if not any(re.search(r"[가-힣]", name) for name in result):
            for name in result[:2]:
                params = {"action": "wbsearchentities", "search": name,
                          "language": "ja" if re.search(r"[ぁ-んァ-ヶ一-龯]", name) else "en",
                          "uselang": "ko", "format": "json", "limit": "5"}
                try:
                    data = self.http.get_json("https://www.wikidata.org/w/api.php?" + urlencode(params))
                    labels = [entry.get("display", {}).get("label", {}).get("value", "") for entry in data.get("search", [])
                              if normal(entry.get("match", {}).get("text", "")) == normal(name)
                              and entry.get("display", {}).get("label", {}).get("language") == "ko"]
                    labels = list(dict.fromkeys(s for s in labels if re.search(r"[가-힣]", s)))
                    if len(labels) == 1:
                        result.append(labels[0])
                        break
                except (SourceError, AttributeError, TypeError) as exc:
                    warnings.append(f"Wikidata 한국어명: {exc}")
                    break
        return list(dict.fromkeys(n for n in result if n and len(normal(n)) >= 2))[:8]

    def _catalog_items(self, warnings: list[str]) -> list[dict]:
        if self._catalog is not None:
            return self._catalog
        first = self.http.get_json("https://api.anissia.net/anime/list/0")
        data = first.get("data") or {}
        pages = data.get("totalPages", 1)
        if not isinstance(pages, int) or not 1 <= pages <= 200:
            raise SourceError("애니시아 목록 페이지 범위를 확인할 수 없음")
        items = list(data.get("content") or [])
        for page in range(1, pages):
            try:
                res = self.http.get_json(f"https://api.anissia.net/anime/list/{page}")
                items.extend((res.get("data") or {}).get("content") or [])
            except SourceError as exc:
                warnings.append(f"애니시아 전체 목록 {page}페이지: {exc}")
                break
        self._catalog = items
        return items

    def _anime(self, names: list[str], season: int | None, warnings: list[str]) -> list[dict]:
        collected = {}
        for name in names[:4]:
            try:
                params = urlencode({"q": re.sub(r"(?:season|시즌)\s*\d+", "", name, flags=re.I).strip()})
                data = self.http.get_json(f"https://api.anissia.net/anime/list/0?{params}")
                for item in (data.get("data") or {}).get("content") or []:
                    collected[item.get("animeNo")] = item
            except (SourceError, AttributeError) as exc:
                warnings.append(f"애니시아 작품 검색: {exc}")
        ranked = self._rank(collected.values(), names, season)
        if not ranked:
            try:
                ranked = self._rank(self._catalog_items(warnings), names, season)
            except SourceError as exc:
                warnings.append(f"애니시아 작품 목록: {exc}")
        return ranked[:4]

    @staticmethod
    def _rank(items, names: list[str], season: int | None):
        scored = []
        wanted = [normal(n) for n in names]
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("animeNo"), int):
                continue
            titles = [item.get("subject", ""), item.get("originalSubject", "")]
            if season and season_of(titles[0]) and season_of(titles[0]) != season:
                continue
            values = [normal(t) for t in titles if isinstance(t, str)]
            exact = any(n and n in values for n in wanted)
            close = any(len(n) >= 4 and (n in v or v in n) and len(v) >= 4 for n in wanted for v in values)
            if exact or close:
                scored.append((0 if exact else 1, item))
        return [item for _, item in sorted(scored, key=lambda p: p[0])]

    def _candidates_from_naver_post(self, url: str, aliases: list[str], query: Query, creator: str):
        html = self.http.get_text(url)
        doc = Document(html)
        for frame in doc.frames:
            target = urljoin(url, frame)
            if _naver_url(target) == url:
                html = self.http.get_text(target)
                doc = Document(html)
                break
        title = doc.meta.get("og:title", "")
        confidence = matching_post(title, aliases, query.season, query.episode)
        if confidence == "none":
            return []
        files = []
        script_pattern = r"aPostFiles\[\d+\]\s*=\s*JSON\.parse\(\s*'((?:\\.|[^'\\])*)'"
        for match in re.finditer(script_pattern, html):
            try:
                rows = json.loads(_decode_js_string(match.group(1)))
                for row in rows if isinstance(rows, list) else []:
                    if row.get("maliciousCodeYn") == "true" or row.get("punishType") not in (None, "0"):
                        continue
                    files.append((unquote(row.get("encodedAttachFileName", "")), row.get("encodedAttachFileUrl", "")))
            except (ValueError, TypeError, AttributeError):
                continue
        for link in doc.links:
            if _host(link.href) in ("download.blog.naver.com", "blogfiles.pstatic.net"):
                files.append((link.download or link.text or file_name_from_url(link.href), link.href))
        candidates = []
        for name, link in dict.fromkeys(files):
            if not allowed_url(link) or _host(link) not in ("download.blog.naver.com", "blogfiles.pstatic.net") or not suitable_file(name, query.episode):
                continue
            candidates.append(Candidate("ReAnime/네이버", title, name, url, link,
                                        season=query.season or season_of(title), episode=query.episode or episode_of(title),
                                        creator=creator, confidence=confidence, page_title=title))
        return candidates

    def _naver(self, source: str, aliases: list[str], query: Query, creator: str):
        canonical = _naver_url(source)
        if not canonical:
            return []
        try:
            result = self._candidates_from_naver_post(canonical, aliases, query, creator)
        except SourceError:
            result = []  # A deleted latest post need not hide the creator's other posts.
        if result:
            return result
        blog_id = urlsplit(canonical).path.strip("/").split("/")[0]
        for alias in aliases[:3]:
            base = "https://blog.naver.com/PostSearchList.naver?" + urlencode({"blogId": blog_id, "SearchText": alias})
            queue = [base]
            visited = set()
            while queue and len(visited) < 5:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                doc = Document(self.http.get_text(url))
                posts = []
                for link in doc.links:
                    target = urljoin(url, link.href)
                    post = _naver_url(target)
                    if post and urlsplit(post).path.strip("/").split("/")[0] == blog_id and matching_post(link.text, aliases, query.season, query.episode) != "none":
                        posts.append(post)
                    parsed = urlsplit(target)
                    if parsed.hostname == "blog.naver.com" and parsed.path == "/PostSearchList.naver" and target not in visited and len(queue) < 4:
                        params = parse_qs(parsed.query)
                        if params.get("blogId") == [blog_id] and params.get("SearchText") == [alias] and (params.get("page") or params.get("currentPage")):
                            queue.append(target)
                for post in list(dict.fromkeys(posts))[:6]:
                    result.extend(self._candidates_from_naver_post(post, aliases, query, creator))
                if result:
                    return result
        return result

    def _tistory_post(self, url: str, aliases: list[str], query: Query, creator: str):
        html = self.http.get_text(url)
        doc = Document(html)
        title = doc.meta.get("og:title", "")
        confidence = matching_post(title, aliases, query.season, query.episode)
        if confidence == "none":
            return []
        result = []
        for link in doc.links:
            file_url = urljoin(url, link.href)
            if _host(file_url) != "blog.kakaocdn.net":
                continue
            name = file_name_from_url(file_url)
            if suitable_file(name, query.episode):
                result.append(Candidate("ReAnime/티스토리", title, name, url, file_url,
                                        season=query.season or season_of(title), episode=query.episode or episode_of(title),
                                        creator=creator, confidence=confidence, page_title=title))
        return result

    def _tistory(self, source: str, aliases: list[str], query: Query, creator: str):
        parsed = urlsplit(source)
        origin = f"https://{parsed.hostname}"
        if re.fullmatch(r"/(?:\d+|entry/[^/]+)/?", parsed.path):
            try:
                result = self._tistory_post(source, aliases, query, creator)
            except SourceError:
                result = []
            if result:
                return result
        for alias in aliases[:3]:
            base = f"{origin}/search/{quote(alias, safe='')}"
            queue = [base]
            visited = set()
            result = []
            while queue and len(visited) < 5:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                doc = Document(self.http.get_text(url))
                posts = []
                for link in doc.links:
                    target = urljoin(url, link.href)
                    path = urlsplit(target).path
                    if _host(target) != parsed.hostname:
                        continue
                    if re.fullmatch(r"/(?:\d+|entry/[^/]+)/?", path) and matching_post(link.text, aliases, query.season, query.episode) != "none":
                        posts.append(target.split("#")[0])
                    if path == urlsplit(base).path and parse_qs(urlsplit(target).query).get("page") and len(queue) < 4:
                        queue.append(target)
                for post in list(dict.fromkeys(posts))[:6]:
                    result.extend(self._tistory_post(post, aliases, query, creator))
                if result:
                    return result
        return []

    def _blogger_content(self, content: str, title: str, source: str, aliases: list[str], query: Query, creator: str):
        confidence = matching_post(title, aliases, query.season, query.episode)
        if confidence == "none":
            return []
        result = []
        for link in Document(content).links:
            if not drive_id(link.href):
                continue
            label = link.text or "Google Drive 자막"
            if re.search(r"폰트|서체|글꼴|fonts?|\.ttf|\.otf", label, re.I):
                continue
            label_season = season_of(label)
            if query.season and label_season and label_season != query.season:
                continue
            if query.episode:
                ep = episode_of(label)
                if ep is not None and ep != query.episode:
                    continue
            result.append(Candidate("ReAnime/Blogger", title, label[:100] + ".zip" if not re.search(r"\.(?:smi|srt|vtt|ass|zip|7z|rar)$", label, re.I) else label,
                                    source, link.href, season=query.season or season_of(title), episode=query.episode or episode_of(title),
                                    creator=creator, confidence="review", note="Drive 링크의 실제 파일명은 다운로드 때 확인"))
        return result

    def _blogger(self, source: str, aliases: list[str], query: Query, creator: str):
        origin = f"https://{_host(source)}"
        result = []
        if re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html", urlsplit(source).path):
            try:
                html = self.http.get_text(source)
                doc = Document(html)
                result.extend(self._blogger_content(html, doc.meta.get("og:title", doc.title), source, aliases, query, creator))
            except SourceError:
                pass
            if result:
                return result
        for alias in aliases[:3]:
            url = f"{origin}/feeds/posts/default?" + urlencode({"alt": "json", "max-results": 100, "q": alias})
            try:
                feed = self.http.get_json(url)
                for entry in (feed.get("feed") or {}).get("entry") or []:
                    title = (entry.get("title") or {}).get("$t", "")
                    page = next((x.get("href", source) for x in entry.get("link", []) if x.get("rel") == "alternate"), source)
                    result.extend(self._blogger_content((entry.get("content") or {}).get("$t", ""), title, page, aliases, query, creator))
            except (SourceError, AttributeError, TypeError):
                search = f"{origin}/search?{urlencode({'q': alias})}"
                queue, visited = [search], set()
                while queue and len(visited) < 3:
                    page = queue.pop(0)
                    if page in visited:
                        continue
                    visited.add(page)
                    doc = Document(self.http.get_text(page))
                    for link in doc.links:
                        target = urljoin(page, link.href)
                        if _host(target) != _host(origin):
                            continue
                        if re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html", urlsplit(target).path) and matching_post(link.text, aliases, query.season, query.episode) != "none":
                            html = self.http.get_text(target)
                            article = Document(html)
                            result.extend(self._blogger_content(html, article.meta.get("og:title", article.title), target, aliases, query, creator))
                        if urlsplit(target).path == "/search" and parse_qs(urlsplit(target).query).get("q") == [alias] and target not in visited and len(queue) < 2:
                            queue.append(target)
            if result:
                break
        return result

    def _read_source(self, source: str, aliases: list[str], query: Query, creator: str):
        source = self._https_source(source)
        host = _host(source)
        if not allowed_url(source):
            return []
        if _naver_url(source):
            return self._naver(source, aliases, query, creator)
        if host.endswith(".tistory.com"):
            return self._tistory(source, aliases, query, creator)
        if host.endswith(".blogspot.com"):
            return self._blogger(source, aliases, query, creator)
        return []

    @staticmethod
    def _https_source(url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme == "http" and not parsed.username and not parsed.password and parsed.port is None:
            upgraded = parsed._replace(scheme="https").geturl()
            if allowed_url(upgraded):
                return upgraded
        return url

    def direct(self, url: str, query: Query) -> SearchResult:
        """User-supplied post or attachment link from Edge or the desktop UI."""
        url = self._https_source(url.strip())
        result = SearchResult()
        if not allowed_url(url):
            return SearchResult(status="error", warnings=["지원하는 HTTPS 링크를 입력해 줘"])
        host = _host(url)
        try:
            if host in ("download.blog.naver.com", "blogfiles.pstatic.net", "blog.kakaocdn.net"):
                name = file_name_from_url(url)
                if suitable_file(name, query.episode):
                    result.candidates.append(Candidate("직접 첨부", query.title, name, url, url,
                                                       season=query.season, episode=query.episode))
            elif drive_id(url):
                result.candidates.append(Candidate("직접 Drive", query.title, "Google Drive 파일", url, url,
                                                   season=query.season, episode=query.episode))
            else:
                result.candidates.extend(self._read_source(url, [query.title], query, "사용자 링크"))
        except SourceError as exc:
            result.warnings.append(str(exc))
            result.status = "error"
        if result.candidates:
            result.status = "review"
        return result

    def _google(self, aliases: list[str], query: Query, warnings: list[str]):
        seen = set()
        for name in aliases[:3]:
            for q in ([f"{name} {query.episode}화 자막", f"{name} 자막"] if query.episode else [f"{name} 자막"]):
                address = "https://www.google.com/search?" + urlencode({"hl": "ko", "q": q})
                try:
                    html = self.http.get_text(address)
                    if re.search(r"captcha-form|recaptcha|/sorry/|unusual traffic", html, re.I):
                        raise SourceError("Google 자동 검색 제한. 브라우저에서 검색 후 글 주소를 앱에 붙여넣어 줘")
                    page_urls = []
                    for link in Document(html).links:
                        if not link.heading or matching_post(link.text, aliases, query.season, query.episode) == "none":
                            continue
                        target = urljoin("https://www.google.com", link.href)
                        if urlsplit(target).path == "/url" and _host(target) == "www.google.com":
                            params = parse_qs(urlsplit(target).query)
                            target = params.get("q", params.get("url", [""]))[0]
                        if _host(target) not in ("blog.naver.com", "m.blog.naver.com") and not _host(target).endswith((".tistory.com", ".blogspot.com")):
                            continue
                        if allowed_url(target) and target not in seen:
                            page_urls.append(target)
                except SourceError as exc:
                    warnings.append(f"Google 공개 검색: {exc}")
                    return
                for target in page_urls[:6]:
                    seen.add(target)
                    yield target
                if len(seen) >= 12:
                    return

    def search(self, query: Query) -> SearchResult:
        warnings = []
        alias_warnings = []
        if not query.title.strip():
            return SearchResult(status="error", warnings=["작품명을 입력해 줘"])
        names = self.names(query, alias_warnings)
        works = self._anime(names, query.season, warnings)
        return self._search_works(query, works, names, alias_warnings, warnings)

    def search_selected(self, query: Query, anime: Anime) -> SearchResult:
        """Search subtitles for an explicitly selected Anissia work."""
        if anime.anime_no <= 0 or not anime.subject.strip():
            return SearchResult(status="error", warnings=["선택한 작품 정보가 올바르지 않아"])
        selected = Query(anime.subject, query.language, query.season, query.episode, query.slug)
        names = list(dict.fromkeys(name for name in (anime.subject, anime.original_subject) if name))
        work = {"animeNo": anime.anime_no, "subject": anime.subject,
                "originalSubject": anime.original_subject}
        return self._search_works(selected, [work], names, [], [])

    def _search_works(self, query: Query, works: list[dict], names: list[str],
                      alias_warnings: list[str], warnings: list[str]) -> SearchResult:
        found: list[Candidate] = []
        for work in works:
            ident = work["animeNo"]
            subject = work.get("subject", query.title)
            aliases = list(dict.fromkeys([subject, work.get("originalSubject", ""), *names]))
            try:
                captions = self.http.get_json(f"https://api.anissia.net/anime/caption/animeNo/{ident}")
            except SourceError as exc:
                warnings.append(f"애니시아 {subject}: {exc}")
                continue
            for creator in (captions.get("data") or [])[:30]:
                source = creator.get("website", "")
                if not source:
                    continue
                try:
                    found.extend(self._read_source(source, aliases, query, creator.get("name", "")))
                except (SourceError, ValueError) as exc:
                    warnings.append(f"{creator.get('name', '제작자')}: {exc}")
        if not found:
            for url in self._google(names, query, warnings):
                try:
                    found.extend(self._read_source(url, names, query, _host(url)))
                except (SourceError, ValueError) as exc:
                    warnings.append(f"{_host(url)}: {exc}")
                if found:
                    break
        unique = list({candidate.key: candidate for candidate in found}.values())
        unique.sort(key=lambda c: (c.confidence != "exact", c.provider, c.title))
        # Failure to enrich a title is advisory; completed source searches can
        # still establish an empty result and trigger OpenSubtitles fallback.
        return SearchResult(unique, alias_warnings + warnings,
                            "found" if unique else "error" if warnings else "empty")
