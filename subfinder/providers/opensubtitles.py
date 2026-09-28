"""OpenSubtitles.com REST API (the app's own key is entered in settings)."""

import threading
import time
from urllib.parse import urlencode, urlsplit

from ..matching import ARCHIVES, FORMATS
from ..models import Candidate, Query, SearchResult
from ..network import HttpClient, SourceError


class OpenSubtitles:
    BASE = "https://api.opensubtitles.com/api/v1"

    def __init__(self, http: HttpClient | None = None, api_key: str = ""):
        self.http = http or HttpClient()
        self.api_key = api_key.strip()
        self._api_lock = threading.Lock()
        self._next_request = 0.0

    def _api_json(self, url: str, *, method: str = "GET", payload: dict | None = None):
        # OpenSubtitles limits API traffic. Serialize requests, including an
        # immediate download after a search, to avoid avoidable HTTP 429s.
        with self._api_lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + 1.05
            return self.http.get_json(url, method=method, payload=payload, headers=self._headers())

    def _headers(self):
        if not self.api_key:
            raise SourceError("OpenSubtitles API 키가 없음. 설정에서 본인 키를 입력해 줘")
        return {"Api-Key": self.api_key, "User-Agent": "AnimeSubtitleFinder v0.1.0",
                "Accept": "application/json"}

    def search(self, query: Query) -> SearchResult:
        try:
            self._headers()
        except SourceError as exc:
            return SearchResult(status="error", warnings=[str(exc)])
        params = {"query": query.title.strip(), "languages": query.language,
                  **({"type": "episode"} if query.season or query.episode else {}),
                  **({"season_number": query.season} if query.season else {}),
                  **({"episode_number": query.episode} if query.episode else {})}
        found: list[Candidate] = []
        warnings = []
        for page in (1, 2, 3):
            try:
                data = self._api_json(f"{self.BASE}/subtitles?{urlencode({**params, 'page': page})}")
            except SourceError as exc:
                warnings.append(f"OpenSubtitles 검색: {exc}")
                return SearchResult(found, warnings, "found" if found else "error")
            for item in data.get("data", [])[:100]:
                attrs = item.get("attributes") or {}
                details = attrs.get("feature_details") or {}
                lang = attrs.get("language", "")
                if query.language and lang and lang.casefold() != query.language.casefold():
                    continue
                for f in (attrs.get("files") or [])[:20]:
                    ident = f.get("file_id")
                    file_name = f.get("file_name", "")
                    if not isinstance(ident, int) or ident <= 0 or not isinstance(file_name, str):
                        continue
                    if not any(file_name.lower().endswith(ext) for ext in FORMATS | ARCHIVES):
                        continue
                    result_title = details.get("title") or details.get("movie_name") or query.title
                    result_season = details.get("season_number") if isinstance(details.get("season_number"), int) else None
                    result_episode = details.get("episode_number") if isinstance(details.get("episode_number"), int) else None
                    if query.season and result_season and query.season != result_season:
                        continue
                    if query.episode and result_episode and query.episode != result_episode:
                        continue
                    confidence = ("exact" if query.episode and result_episode == query.episode
                                  and (not query.season or result_season == query.season) else "review")
                    found.append(Candidate("OpenSubtitles", result_title, file_name,
                                           "https://www.opensubtitles.com", language=lang or query.language,
                                           season=result_season, episode=result_episode, confidence=confidence,
                                           creator=(attrs.get("uploader") or {}).get("name", ""),
                                           note=f"다운로드 {attrs.get('download_count', 0)} · {attrs.get('release', '')}",
                                           file_id=ident))
            total = data.get("total_pages", 1)
            if not isinstance(total, int) or page >= total or len(found) >= 150:
                break
        unique = list({candidate.key: candidate for candidate in found}.values())
        return SearchResult(unique, warnings, "found" if unique else "empty")

    def download(self, candidate: Candidate) -> tuple[bytes, str]:
        if not isinstance(candidate.file_id, int) or candidate.file_id <= 0:
            raise SourceError("OpenSubtitles 파일 ID가 올바르지 않음")
        payload = self._api_json(f"{self.BASE}/download", method="POST",
                                 payload={"file_id": candidate.file_id})
        link = payload.get("link", "")
        parsed = urlsplit(link)
        if parsed.scheme != "https" or parsed.hostname != "www.opensubtitles.com" or parsed.port not in (None, 443):
            raise SourceError("OpenSubtitles 다운로드 주소가 올바르지 않음")
        response = self.http.get_bytes(link)
        if urlsplit(response.url).hostname != "www.opensubtitles.com":
            raise SourceError("OpenSubtitles 다운로드 주소가 변경됨")
        return response.body, payload.get("file_name") or candidate.file_name
