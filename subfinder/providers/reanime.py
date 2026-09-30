"""Anissia and registered creator blogs for public subtitle discovery.

The adapter extracts public attachment links. It does not contain subtitle
files, reuse the ReAnime extension, or execute scripts from source pages.
"""

import json
import re
import unicodedata
from dataclasses import replace
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

from ..htmlparse import Document
from ..matching import (blog_title_alias, drive_id, episode_of, episode_range, file_name_from_url,
                        is_special, matching_post, matching_public_title, normal, season_of, suitable_file,
                        title_queries, without_mixed_specials)
from ..models import Anime, AnimeSearchResult, Candidate, CreatorBlog, Query, SearchResult
from ..network import HttpClient, SourceError, allowed_url


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _creator_blog_url(website: str) -> str | None:
    """Turn an Anissia caption post URL into its creator's blog home page."""
    if not isinstance(website, str):
        return None
    try:
        parsed = urlsplit(website.strip())
        if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)):
            return None
        host = (parsed.hostname or "").lower()
        if host in ("blog.naver.com", "m.blog.naver.com"):
            params = parse_qs(parsed.query)
            parts = parsed.path.strip("/").split("/")
            blog_id = params.get("blogId", [parts[0] if parts else ""])[0]
            if re.fullmatch(r"[\w-]+", blog_id) and blog_id not in ("PostView.naver", "PostSearchList.naver"):
                return f"https://blog.naver.com/{blog_id}"
        elif host.endswith((".blogspot.com", ".tistory.com")):
            homepage = f"https://{host}"
            if allowed_url(homepage):
                return homepage + "/"
    except ValueError:
        return None
    return None


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


def _post_confidence(title: str, aliases: list[str], query: Query) -> str:
    explicit = matching_post(title, aliases, query.season, query.episode)
    if explicit != "none":
        return explicit
    # Whole-series posts may contain a numbered attachment; keep those for review.
    return ("review" if matching_public_title(title, aliases, query.season,
                                               query.episode) != "none" else "none")


def _blog_search_titles(title: str, aliases: list[str], query: Query,
                        search_terms: list[str] | None):
    """Use the registered post's confirmed short name only for this creator."""
    short = blog_title_alias(title, aliases, query.season)
    if short:
        return list(dict.fromkeys([*aliases, short])), [short]
    return aliases, search_terms


def _family_title(title: str) -> str:
    """Remove a confirmed season suffix for searching the same creator's older posts."""
    value = unicodedata.normalize("NFKC", title)
    value = re.sub(
        r"(?:season|시즌)\s*\d{1,2}(?:\s*(?:기|期))?"
        r"|(?:[제第]\s*)?\d{1,2}\s*(?:기|期)"
        r"|\b\d{1,2}(?:st|nd|rd|th)\s+season\b",
        " ", value, flags=re.I)
    value = re.sub(
        r"(?:\s+|(?<=[가-힣ぁ-んァ-ヶ一-龯]))(?:VIII|VII|III|VI|IV|IX|II|V|X)"
        r"(?=\s*(?:$|[:~～〜]|[-–—]\s|Part\b|Cour\b))",
        " ", value)
    value = re.sub(r"\s+\d{1,2}(?:st|nd|rd|th)\s*$|\s+[2-9]\s*$", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


class ReAnime:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()
        self._catalog: list[dict] | None = None

    def creator_blogs(self, anime: Anime) -> list[CreatorBlog]:
        """List creator blog home pages from the selected Anissia work's captions."""
        if anime.anime_no <= 0:
            raise SourceError("선택한 작품 정보가 올바르지 않아")
        response = self.http.get_json(f"https://api.anissia.net/anime/caption/animeNo/{anime.anime_no}")
        if response.get("code", "ok") != "ok" or not isinstance(response.get("data"), list):
            raise SourceError("애니시아 제작자 목록 응답 오류")
        blogs: list[CreatorBlog] = []
        seen: set[str] = set()
        for creator in response["data"]:
            if not isinstance(creator, dict):
                continue
            url = _creator_blog_url(creator.get("website", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            name = creator.get("name", "")
            blogs.append(CreatorBlog(name.strip() if isinstance(name, str) and name.strip() else "제작자", url))
        return blogs

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
        if first.get("code", "ok") != "ok":
            raise SourceError("애니시아 작품 목록 응답 오류")
        data = first.get("data") or {}
        pages = data.get("totalPages", 1)
        if not isinstance(pages, int) or not 1 <= pages <= 200 or not isinstance(data.get("content"), list):
            raise SourceError("애니시아 목록 페이지 범위를 확인할 수 없음")
        items = list(data.get("content") or [])
        for page in range(1, pages):
            res = self.http.get_json(f"https://api.anissia.net/anime/list/{page}")
            if res.get("code", "ok") != "ok" or not isinstance((res.get("data") or {}).get("content"), list):
                raise SourceError(f"애니시아 전체 목록 {page}페이지 응답 오류")
            items.extend(res["data"]["content"])
        self._catalog = items
        return items

    def _anime(self, names: list[str], season: int | None, warnings: list[str]) -> list[dict]:
        collected = {}
        for name in names[:4]:
            try:
                params = urlencode({"q": re.sub(r"(?:season|시즌)\s*\d+", "", name, flags=re.I).strip()})
                data = self.http.get_json(f"https://api.anissia.net/anime/list/0?{params}")
                if data.get("code", "ok") != "ok" or not isinstance((data.get("data") or {}).get("content"), list):
                    raise SourceError("애니시아 작품 검색 응답 오류")
                for item in (data.get("data") or {}).get("content") or []:
                    if isinstance(item, dict) and isinstance(item.get("animeNo"), int):
                        collected[item["animeNo"]] = item
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
        if any(score == 0 for score, _ in scored):
            scored = [(score, item) for score, item in scored if score == 0]
        return [item for _, item in sorted(scored, key=lambda p: p[0])]

    def _naver_post_document(self, url: str):
        html = self.http.get_text(url)
        doc = Document(html)
        for frame in doc.frames:
            target = urljoin(url, frame)
            if _naver_url(target) == url:
                html = self.http.get_text(target)
                doc = Document(html)
                break
        return html, doc

    def _candidates_from_naver_post(self, url: str, aliases: list[str], query: Query, creator: str,
                                    post_data: tuple[str, Document] | None = None):
        html, doc = post_data if post_data is not None else self._naver_post_document(url)
        title = doc.meta.get("og:title") or doc.title
        confidence = _post_confidence(title, aliases, query)
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
            if (not allowed_url(link) or _host(link) not in ("download.blog.naver.com", "blogfiles.pstatic.net")
                    or not suitable_file(name, query.episode)
                    or (not is_special(query.title) and is_special(without_mixed_specials(name)))):
                continue
            candidates.append(Candidate("ReAnime/네이버", title, name, url, link,
                                        season=query.season or season_of(title), episode=query.episode or episode_of(title),
                                        creator=creator, confidence=confidence, page_title=title,
                                        require_episode=confidence != "exact"))
        return candidates

    def _naver(self, source: str, aliases: list[str], query: Query, creator: str,
               search_terms: list[str] | None = None):
        canonical = _naver_url(source)
        if not canonical:
            return []
        try:
            post_data = self._naver_post_document(canonical)
            doc = post_data[1]
            aliases, search_terms = _blog_search_titles(doc.meta.get("og:title") or doc.title,
                                                        aliases, query, search_terms)
            result = self._candidates_from_naver_post(canonical, aliases, query, creator, post_data)
        except SourceError:
            result = []  # A deleted latest post need not hide the creator's other posts.
        if result and query.episode is not None:
            return result
        blog_id = urlsplit(canonical).path.strip("/").split("/")[0]
        seen_posts = {canonical} if result else set()
        for alias in (search_terms or aliases)[:6]:
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
                    if post and urlsplit(post).path.strip("/").split("/")[0] == blog_id and _post_confidence(link.text, aliases, query) != "none":
                        posts.append(post)
                    parsed = urlsplit(target)
                    if parsed.hostname == "blog.naver.com" and parsed.path == "/PostSearchList.naver" and target not in visited and len(queue) < 4:
                        params = parse_qs(parsed.query)
                        if params.get("blogId") == [blog_id] and params.get("SearchText") == [alias] and (params.get("page") or params.get("currentPage")):
                            queue.append(target)
                for post in list(dict.fromkeys(posts))[:6]:
                    if post in seen_posts:
                        continue
                    seen_posts.add(post)
                    result.extend(self._candidates_from_naver_post(post, aliases, query, creator))
                if result and query.episode is not None:
                    return result
                if len(seen_posts) >= 100:
                    return list({candidate.key: candidate for candidate in result}.values())
        return list({candidate.key: candidate for candidate in result}.values())

    def _tistory_post(self, url: str, aliases: list[str], query: Query, creator: str,
                       doc: Document | None = None):
        if doc is None:
            doc = Document(self.http.get_text(url))
        title = doc.meta.get("og:title") or doc.title
        confidence = _post_confidence(title, aliases, query)
        if confidence == "none":
            return []
        result = []
        for link in doc.links:
            file_url = urljoin(url, link.href)
            if _host(file_url) != "blog.kakaocdn.net":
                continue
            name = file_name_from_url(file_url)
            if (suitable_file(name, query.episode)
                    and (is_special(query.title) or not is_special(without_mixed_specials(name)))):
                result.append(Candidate("ReAnime/티스토리", title, name, url, file_url,
                                        season=query.season or season_of(title), episode=query.episode or episode_of(title),
                                        creator=creator, confidence=confidence, page_title=title,
                                        require_episode=confidence != "exact"))
        return result

    def _tistory(self, source: str, aliases: list[str], query: Query, creator: str,
                  search_terms: list[str] | None = None):
        parsed = urlsplit(source)
        origin = f"https://{parsed.hostname}"
        result = []
        seen_posts = set()
        if re.fullmatch(r"/(?:\d+|entry/[^/]+)/?", parsed.path):
            try:
                doc = Document(self.http.get_text(source))
                aliases, search_terms = _blog_search_titles(doc.meta.get("og:title") or doc.title,
                                                            aliases, query, search_terms)
                result.extend(self._tistory_post(source, aliases, query, creator, doc))
            except SourceError:
                pass
            if result and query.episode is not None:
                return result
            if result:
                seen_posts.add(source)
        for alias in (search_terms or aliases)[:6]:
            base = f"{origin}/search/{quote(alias, safe='')}"
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
                    path = urlsplit(target).path
                    if _host(target) != parsed.hostname:
                        continue
                    title = (link.attributes.get("data-tiara-copy")
                             or link.attributes.get("data-tiara-name") or link.text)
                    if link.attributes.get("onclick"):
                        title = re.sub(r"^\s*\d{4}[./-]\d{1,2}[./-]\d{1,2}\s*", "", title)
                    if re.fullmatch(r"/(?:\d+|entry/[^/]+)/?", path) and _post_confidence(title, aliases, query) != "none":
                        posts.append(target.split("#")[0])
                    if path == urlsplit(base).path and parse_qs(urlsplit(target).query).get("page") and len(queue) < 4:
                        queue.append(target)
                for post in list(dict.fromkeys(posts))[:6]:
                    if post in seen_posts:
                        continue
                    seen_posts.add(post)
                    result.extend(self._tistory_post(post, aliases, query, creator))
                if result and query.episode is not None:
                    return result
                if len(seen_posts) >= 100:
                    return list({candidate.key: candidate for candidate in result}.values())
        return list({candidate.key: candidate for candidate in result}.values())

    def _tistory_offset_candidates(self, source: str, subject: str, query: Query,
                                   creator: dict) -> list[Candidate]:
        """Offer a season-to-cumulative episode mapping only when the latest post proves it."""
        season = season_of(subject)
        if (query.episode is None or season is None or season <= 1
                or query.season not in (None, season)):
            return []
        try:
            registered_latest = int(creator.get("episode", 0))
        except (TypeError, ValueError):
            return []
        source = self._https_source(source)
        family = _family_title(subject)
        if (not allowed_url(source) or not 0 < query.episode <= registered_latest <= 999 or not family
                or normal(family) == normal(subject)
                or not re.fullmatch(r"/(?:\d+|entry/[^/]+)/?", urlsplit(source).path)):
            return []
        article = Document(self.http.get_text(source))
        latest_title = article.meta.get("og:title") or article.title
        latest_source_episode = episode_of(latest_title)
        if (latest_source_episode is None
                or matching_post(latest_title, [family], None, latest_source_episode) != "exact"):
            return []
        offset = latest_source_episode - registered_latest
        source_episode = query.episode + offset
        if offset <= 0 or source_episode <= 0 or source_episode > 999:
            return []
        alternate = self._tistory(source, [family],
                                   Query(family, query.language, None, source_episode, query.slug),
                                   creator.get("name", ""), [family])
        note = (f"시즌 {season} {query.episode}화 / 원문 통산 {source_episode}화 후보. "
                "애니시아 최신 화와 원문 번호 차이로 찾았습니다. 시즌을 확인해 주세요.")
        return [replace(candidate, title=f"{candidate.title} · 시즌 {query.episode}화 / 통산 {source_episode}화",
                        season=season, episode=query.episode, source_episode=source_episode,
                        confidence="review", require_episode=True, note=note)
                for candidate in alternate
                if (candidate.episode == source_episode
                    and matching_post(candidate.page_title, [family], None, source_episode) == "exact")]

    def _blogger_content(self, content: str, title: str, source: str, aliases: list[str], query: Query, creator: str):
        confidence = _post_confidence(title, aliases, query)
        if confidence == "none":
            return []
        result = []
        for link in Document(content).links:
            if not drive_id(link.href):
                continue
            label = link.text or "Google Drive 자막"
            if re.search(r"폰트|서체|글꼴|fonts?|\.ttf|\.otf", label, re.I):
                continue
            if not is_special(query.title) and is_special(label):
                continue
            label_season = season_of(label)
            if query.season and label_season and label_season != query.season:
                continue
            if query.episode:
                number_range = episode_range(label)
                if number_range is None:
                    found_range = re.search(r"(?:^|[\s(\[])(\d{1,3})\s*[~～〜\-–]\s*(\d{1,3})(?=\s|[)\]]|$)", label)
                    if found_range:
                        number_range = (int(found_range.group(1)), int(found_range.group(2)))
                if number_range and not number_range[0] <= query.episode <= number_range[1]:
                    continue
                ep = episode_of(label)
                if ep is not None and ep != query.episode and not number_range:
                    continue
            result.append(Candidate("ReAnime/Blogger", title, label[:100] + ".zip" if not re.search(r"\.(?:smi|srt|vtt|ass|zip|7z|rar)$", label, re.I) else label,
                                    source, link.href, season=query.season or season_of(title), episode=query.episode or episode_of(title),
                                    creator=creator, confidence="review", note="Drive 링크의 실제 파일명은 다운로드 때 확인",
                                    require_episode=True))
        # A numbered post with one matching attachment identifies the requested
        # episode. Bundles and ambiguous posts still need download-time review.
        if (query.episode is not None and confidence == "exact" and len(result) == 1
                and episode_range(title) is None):
            result[0].confidence = "exact"
            result[0].require_episode = False
        elif query.episode is None:
            numbers = episode_range(title)
            if numbers and 0 < numbers[1] - numbers[0] < 50:
                expanded = []
                for item in result:
                    stem, suffix = item.file_name.rsplit(".", 1)
                    for number in range(numbers[0], numbers[1] + 1):
                        expanded.append(replace(item, file_name=f"{stem} ({number}화).{suffix}",
                                                episode=number))
                result = expanded
        return result

    def _blogger_html_search(self, origin: str, alias: str, aliases: list[str],
                             query: Query, creator: str):
        search = f"{origin}/search?{urlencode({'q': alias})}"
        queue, visited, posts = [search], set(), {}
        while queue and len(visited) < 10 and len(posts) < 100:
            page = queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            doc = Document(self.http.get_text(page))
            for link in doc.links:
                target = urljoin(page, link.href)
                if _host(target) != _host(origin):
                    continue
                parsed = urlsplit(target)
                if (re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html", parsed.path)
                        and _post_confidence(link.text, aliases, query) != "none"):
                    posts.setdefault(target.split("#")[0].split("?")[0], link.text)
                if (parsed.path == "/search" and "blog-pager-older-link" in link.attributes.get("class", "")
                        and parse_qs(parsed.query).get("q") == [alias]
                        and target not in visited and target not in queue):
                    queue.append(target)
            if query.episode is not None and posts:
                break
        result = []
        for post in list(posts)[:100]:
            try:
                html = self.http.get_text(post)
            except SourceError:
                continue
            article = Document(html)
            result.extend(self._blogger_content(html, article.meta.get("og:title") or article.title,
                                                post, aliases, query, creator))
            if query.episode is not None and result:
                break
        return result

    def _blogger(self, source: str, aliases: list[str], query: Query, creator: str,
                 search_terms: list[str] | None = None):
        origin = f"https://{_host(source)}"
        result = []
        errors = []
        if re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html", urlsplit(source).path):
            try:
                html = self.http.get_text(source)
                doc = Document(html)
                aliases, search_terms = _blog_search_titles(doc.meta.get("og:title") or doc.title,
                                                            aliases, query, search_terms)
                result.extend(self._blogger_content(html, doc.meta.get("og:title") or doc.title,
                                                    source, aliases, query, creator))
            except SourceError as exc:
                errors.append(str(exc))
            if result and query.episode is not None:
                return result
        for alias in (search_terms or aliases)[:6]:
            found = []
            feed_failed = False
            try:
                start = 1
                for _ in range(10):
                    params = {"alt": "json", "max-results": 100, "q": alias}
                    if start > 1:
                        params["start-index"] = start
                    feed = self.http.get_json(f"{origin}/feeds/posts/default?" + urlencode(params))
                    body = feed.get("feed") or {}
                    entries = body.get("entry") or []
                    if not isinstance(entries, list):
                        raise SourceError("Blogger 피드 형식 오류")
                    for entry in entries:
                        title = (entry.get("title") or {}).get("$t", "")
                        page = next((link.get("href", "") for link in entry.get("link", [])
                                     if link.get("rel") == "alternate"), "")
                        if (_host(page) != _host(origin)
                                or not re.fullmatch(r"/\d{4}/\d{2}/[^/]+\.html", urlsplit(page).path)):
                            continue
                        found.extend(self._blogger_content((entry.get("content") or {}).get("$t", ""),
                                                            title, page, aliases, query, creator))
                    total_text = (body.get("openSearch$totalResults") or {}).get("$t", "")
                    total = int(total_text) if str(total_text).isdecimal() else None
                    if not entries or (total is not None and start + len(entries) > total) or (total is None and len(entries) < 100):
                        break
                    start += len(entries)
            except (SourceError, AttributeError, TypeError, ValueError) as exc:
                errors.append(str(exc))
                feed_failed = True
            if feed_failed or not found:
                try:
                    found.extend(self._blogger_html_search(origin, alias, aliases, query, creator))
                except SourceError as exc:
                    errors.append(str(exc))
            result.extend(found)
            if result and query.episode is not None:
                break
        if not result and errors:
            raise SourceError("Blogger 검색: " + errors[-1])
        return list({candidate.key: candidate for candidate in result}.values())

    def _read_source(self, source: str, aliases: list[str], query: Query, creator: str,
                     search_terms: list[str] | None = None):
        source = self._https_source(source)
        host = _host(source)
        if not allowed_url(source):
            raise SourceError(f"{host or '알 수 없는 출처'} 자동 검색 미지원")
        if _naver_url(source):
            return self._naver(source, aliases, query, creator, search_terms)
        if host.endswith(".tistory.com"):
            return self._tistory(source, aliases, query, creator, search_terms)
        if host.endswith(".blogspot.com"):
            return self._blogger(source, aliases, query, creator, search_terms)
        raise SourceError(f"{host or '알 수 없는 출처'} 자동 검색 미지원")

    @staticmethod
    def _https_source(url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme == "http" and not parsed.username and not parsed.password and parsed.port is None:
            upgraded = parsed._replace(scheme="https").geturl()
            if allowed_url(upgraded):
                return upgraded
        return url

    def direct(self, url: str, query: Query) -> SearchResult:
        """User-supplied post or attachment link from the desktop UI."""
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
                                                       season=query.season, episode=query.episode,
                                                       require_episode=True))
            elif drive_id(url):
                result.candidates.append(Candidate("직접 Drive", query.title, "Google Drive 파일", url, url,
                                                   season=query.season, episode=query.episode,
                                                   require_episode=True))
            else:
                result.candidates.extend(self._read_source(url, [query.title], query, "사용자 링크"))
        except SourceError as exc:
            result.warnings.append(str(exc))
            result.status = "error"
        if result.candidates:
            result.status = "review"
        return result

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
            aliases = list(dict.fromkeys(name for name in
                                         [subject, work.get("originalSubject", ""), *names] if name))
            source_query = Query(subject, query.language, query.season or season_of(subject),
                                 query.episode, query.slug)
            search_terms = title_queries(subject, aliases)
            if (source_query.season or 1) > 1:
                family = _family_title(subject)
                if family and normal(family) != normal(subject):
                    search_terms.append(family)
            try:
                captions = self.http.get_json(f"https://api.anissia.net/anime/caption/animeNo/{ident}")
                if captions.get("code", "ok") != "ok" or not isinstance(captions.get("data"), list):
                    raise SourceError("애니시아 제작자 목록 응답 오류")
            except SourceError as exc:
                warnings.append(f"애니시아 {subject}: {exc}")
                continue
            for creator in captions["data"][:30]:
                if not isinstance(creator, dict):
                    warnings.append(f"애니시아 {subject}: 제작자 항목 형식 오류")
                    continue
                source = creator.get("website", "")
                if not source:
                    warnings.append(f"{creator.get('name', '제작자')}: 원문 주소 없음")
                    continue
                try:
                    source_candidates = self._read_source(source, aliases, source_query,
                                                          creator.get("name", ""), search_terms)
                except (SourceError, ValueError) as exc:
                    warnings.append(f"{creator.get('name', '제작자')}: {exc}")
                    source_candidates = []
                if (not source_candidates and _host(source).endswith(".tistory.com")
                        and source_query.episode is not None):
                    try:
                        source_candidates = self._tistory_offset_candidates(source, subject, source_query, creator)
                    except (SourceError, ValueError) as exc:
                        warnings.append(f"{creator.get('name', '제작자')}: 통산 화수 확인 실패: {exc}")
                found.extend(source_candidates)
        unique = list({candidate.key: candidate for candidate in found}.values())
        unique.sort(key=lambda c: (c.confidence != "exact", c.provider, c.title))
        # Alias lookup is advisory; source failures make an empty result incomplete.
        return SearchResult(unique, alias_warnings + warnings,
                            ("found" if any(candidate.confidence == "exact" for candidate in unique)
                             else "review" if unique else "error" if warnings else "empty"))
