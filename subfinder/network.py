"""Bounded HTTPS requests to known subtitle sources only."""

from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class SourceError(Exception):
    pass


def _request_url(url: str) -> str:
    """urllib requires ASCII URLs; source pages may have Korean path segments."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")


EXACT_HOSTS = {
    "reanime.to", "api.anissia.net", "graphql.anilist.co", "www.wikidata.org",
    "www.google.com", "blog.naver.com", "m.blog.naver.com",
    "download.blog.naver.com", "blogfiles.pstatic.net", "blog.kakaocdn.net",
    "drive.google.com", "drive.usercontent.google.com",
    "api.opensubtitles.com", "www.opensubtitles.com",
}


def allowed_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        host = (parsed.hostname or "").lower()
        return host in EXACT_HOSTS or (host.endswith((".blogspot.com", ".tistory.com"))
                                          and len(host.split(".")) == 3 and all(x.replace("-", "").isalnum() for x in host.split(".")))
    except ValueError:
        return False


@dataclass
class Response:
    body: bytes
    url: str
    headers: dict[str, str]


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = _request_url(newurl)
        if not allowed_url(newurl):
            raise SourceError("허용되지 않은 주소로 이동하려 해서 요청을 중단함")
        # Never forward an API key or authorization token to a different host.
        origin = urllib.parse.urlsplit(req.full_url).hostname
        target = urllib.parse.urlsplit(newurl).hostname
        if origin != target and any(k.lower() in ("api-key", "authorization") for k in req.headers):
            raise SourceError("인증 헤더를 다른 도메인으로 전달할 수 없음")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(RestrictedRedirect())

    def fetch(self, url: str, *, method: str = "GET", payload: dict | None = None,
              headers: dict | None = None, limit: int = 6 * 1024 * 1024) -> Response:
        if not allowed_url(url):
            raise SourceError("지원하는 HTTPS 출처가 아님")
        url = _request_url(url)
        request_headers = {"User-Agent": "SubtitleFinder v0.1.0", "Accept": "*/*"}
        request_headers.update(headers or {})
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        for attempt in range(2):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    final_url = response.geturl()
                    if not allowed_url(final_url):
                        raise SourceError("최종 응답 주소가 허용된 출처가 아님")
                    size = response.headers.get("Content-Length", "")
                    if size.isdecimal() and int(size) > limit:
                        raise SourceError("응답 크기가 제한을 초과함")
                    body = response.read(limit + 1)
                    if len(body) > limit:
                        raise SourceError("응답 크기가 제한을 초과함")
                    return Response(body, final_url, dict(response.headers))
            except urllib.error.HTTPError as error:
                if error.code >= 500 and attempt == 0:
                    time.sleep(0.2)
                    continue
                raise SourceError(f"HTTP {error.code}: {urllib.parse.urlsplit(url).hostname}") from error
            except (TimeoutError, urllib.error.URLError, OSError) as error:
                raise SourceError(f"연결 실패: {urllib.parse.urlsplit(url).hostname}: {error}") from error
        raise SourceError("요청 실패")

    def get_bytes(self, url: str, limit: int = 6 * 1024 * 1024) -> Response:
        return self.fetch(url, limit=limit)

    def get_text(self, url: str) -> str:
        response = self.fetch(url, limit=3 * 1024 * 1024)
        return response.body.decode("utf-8-sig", errors="replace")

    def get_json(self, url: str, *, method: str = "GET", payload: dict | None = None,
                 headers: dict | None = None) -> dict:
        response = self.fetch(url, method=method, payload=payload, headers=headers, limit=2 * 1024 * 1024)
        try:
            data = json.loads(response.body.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("object required")
            return data
        except (ValueError, UnicodeError) as error:
            raise SourceError("JSON 응답을 읽지 못함") from error
