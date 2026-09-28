"""Download, archive selection, filename safety, and auditable output paths."""

from email.message import Message
from html import unescape
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile

from .matching import ARCHIVES, FORMATS, drive_id, episode_of, file_name_from_url, suitable_file
from .models import Candidate, Query
from .network import HttpClient, SourceError, allowed_url
from .providers.opensubtitles import OpenSubtitles


FILE_LIMIT = 6 * 1024 * 1024
SINGLE_LIMIT = 5 * 1024 * 1024


def safe_name(value: str, fallback: str = "subtitle") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")[:100].strip(" .")
    if value.upper().split(".", 1)[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value or fallback


def _extension(data: bytes, name: str) -> str:
    if data.startswith(b"PK\x03\x04"):
        return ".zip"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if data.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    suffix = Path(name).suffix.lower()
    if suffix in FORMATS:
        return suffix
    head = data[:512].decode("utf-8-sig", errors="ignore").lstrip()
    if head.startswith("WEBVTT"):
        return ".vtt"
    if "[Script Info]" in head or "[Events]" in head:
        return ".ass"
    if re.search(r"<sami|<sync\b", head, re.I):
        return ".smi"
    if re.search(r"\d\d:\d\d:\d\d[,\.]\d+\s*-->", head):
        return ".srt"
    raise SourceError("지원 자막/압축 파일인지 확인할 수 없음")


def _zip_selection(data: bytes, episode: int | None) -> tuple[bytes, str] | None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        items = archive.infolist()
        if len(items) > 256 or sum(info.file_size for info in items) > 64 * 1024 * 1024:
            raise SourceError("압축 파일의 항목/전체 크기가 제한을 초과함")
        choices = []
        for info in items:
            if info.is_dir() or info.file_size > SINGLE_LIMIT or info.flag_bits & 1:
                continue
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if name and Path(name).suffix.lower() in FORMATS and suitable_file(name, episode):
                choices.append((info, name))
        if episode is not None:
            exact = [(i, n) for i, n in choices if episode_of(n) == episode]
            if exact:
                choices = exact
        if len(choices) != 1:
            return None  # Preserve original archive for manual selection.
        info, name = choices[0]
        with archive.open(info) as handle:
            body = handle.read(SINGLE_LIMIT + 1)
        if len(body) > SINGLE_LIMIT:
            raise SourceError("압축 내부 자막 크기가 제한을 초과함")
        return body, name
    except (zipfile.BadZipFile, RuntimeError, EOFError, ValueError) as exc:
        raise SourceError("ZIP 파일을 읽지 못함") from exc


def _seven_zip_selection(data: bytes, suffix: str, episode: int | None) -> tuple[bytes, str] | None:
    binary = (shutil.which("7zz") or shutil.which("7z") or
              next((str(p) for p in (Path(r"C:\Program Files\7-Zip\7z.exe"),
                                    Path(r"C:\Program Files (x86)\7-Zip\7z.exe")) if p.exists()), None))
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="subtitle-archive-") as folder:
        path = Path(folder) / ("source" + suffix)
        path.write_bytes(data)
        listing = subprocess.run([binary, "l", "-slt", "-bd", str(path)], capture_output=True, timeout=20)
        if listing.returncode or len(listing.stdout) > 2 * 1024 * 1024:
            raise SourceError("7-Zip 목록을 확인하지 못함")
        entries = re.split(r"\r?\n\r?\n", listing.stdout.decode("utf-8", errors="replace"))
        choices = []
        for entry in entries:
            fields = dict(re.findall(r"^([^\r\n=]+) = ([^\r\n]*)$", entry, re.M))
            name = fields.get("Path", "")
            if not name or fields.get("Folder") == "+":
                continue
            size = fields.get("Size", "")
            base = PurePosixPath(name.replace("\\", "/")).name
            if size.isdecimal() and int(size) <= SINGLE_LIMIT and Path(base).suffix.lower() in FORMATS and suitable_file(base, episode):
                choices.append((name, base))
        if len(choices) > 256:
            raise SourceError("압축 파일 항목 수가 제한을 초과함")
        if episode is not None:
            exact = [(name, base) for name, base in choices if episode_of(base) == episode]
            if exact:
                choices = exact
        if len(choices) != 1:
            return None
        name, base = choices[0]
        run = subprocess.run([binary, "e", "-so", "-y", "-bd", str(path), name], capture_output=True, timeout=30)
        if run.returncode or len(run.stdout) > SINGLE_LIMIT:
            raise SourceError("압축 내부 자막을 읽지 못함")
        return run.stdout, base


def _disposition_name(headers: dict[str, str]) -> str:
    raw = next((v for k, v in headers.items() if k.lower() == "content-disposition"), "")
    if not raw:
        return ""
    msg = Message()
    msg["content-disposition"] = raw
    value = msg.get_param("filename", header="content-disposition")
    if isinstance(value, tuple):
        from email.utils import collapse_rfc2231_value
        return collapse_rfc2231_value(value)
    return str(value or "")


class Downloader:
    def __init__(self, output: Path, http: HttpClient | None = None, opensubtitles: OpenSubtitles | None = None):
        self.output = Path(output)
        self.http = http or HttpClient()
        self.opensubtitles = opensubtitles or OpenSubtitles(self.http)

    def _drive(self, url: str):
        ident = drive_id(url)
        if not ident:
            raise SourceError("Drive 파일 주소를 확인할 수 없음")
        address = "https://drive.google.com/uc?" + urlencode({"export": "download", "id": ident})
        response = self.http.get_bytes(address, FILE_LIMIT)
        if "text/html" in response.headers.get("Content-Type", "").lower():
            html = response.body.decode("utf-8", errors="replace")
            form = re.search(r'<form\b[^>]*action=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</form>', html, re.I)
            if not form:
                raise SourceError("Drive 파일 공개 설정 또는 다운로드 확인 페이지를 확인해 줘")
            target = unescape(form.group(1))
            if urlsplit(target).hostname != "drive.usercontent.google.com" or urlsplit(target).path != "/download":
                raise SourceError("Drive 확인 링크가 허용한 주소가 아님")
            from urllib.parse import parse_qsl
            params = dict(parse_qsl(urlsplit(target).query))
            for field in re.finditer(r'<input\b([^>]+)>', form.group(2), re.I):
                name = re.search(r'name=["\']([^"\']+)["\']', field.group(1), re.I)
                value = re.search(r'value=["\']([^"\']*)["\']', field.group(1), re.I)
                if name and name.group(1) in ("id", "export", "confirm", "uuid"):
                    params[name.group(1)] = unescape(value.group(1)) if value else ""
            if params.get("id") != ident:
                raise SourceError("Drive 확인 화면의 파일 ID가 다름")
            response = self.http.get_bytes("https://drive.usercontent.google.com/download?" + urlencode(params), FILE_LIMIT)
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise SourceError("Drive가 파일 대신 HTML 화면을 보냄")
        return response

    def download(self, candidate: Candidate, query: Query) -> tuple[Path, str]:
        if candidate.file_id is not None:
            data, name = self.opensubtitles.download(candidate)
        else:
            if not candidate.download_url or not allowed_url(candidate.download_url):
                raise SourceError("다운로드 링크가 허용되지 않음")
            response = (self._drive(candidate.download_url) if drive_id(candidate.download_url)
                        else self.http.get_bytes(candidate.download_url, FILE_LIMIT))
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise SourceError("자막 대신 웹페이지를 받음. 원문에서 공개 설정을 확인해 줘")
            data = response.body
            name = _disposition_name(response.headers) or candidate.file_name or file_name_from_url(response.url)
        if not data or len(data) > FILE_LIMIT:
            raise SourceError("빈 파일 또는 크기 제한 초과")
        suffix = _extension(data, name)
        note = ""
        if suffix == ".zip":
            item = _zip_selection(data, query.episode)
            if item:
                data, name = item
                suffix = _extension(data, name)
            else:
                note = "압축 안에서 자막 한 개를 확정하지 못해 원본 ZIP 저장"
        elif suffix in (".7z", ".rar"):
            item = _seven_zip_selection(data, suffix, query.episode)
            if item:
                data, name = item
                suffix = _extension(data, name)
            else:
                note = "해당 화 자막을 한 개로 확정하지 못해 원본 압축 파일 저장 (7-Zip 설치 시 선택 지원)"
        if len(data) > SINGLE_LIMIT and suffix not in ARCHIVES:
            raise SourceError("자막 파일 크기가 제한을 초과함")
        original = safe_name(Path(name.replace("\\", "/")).name)
        base = safe_name(query.title, "Untitled")
        folder = self.output / base / (f"Season {query.season:02d}" if query.season else "Unsorted")
        folder.mkdir(parents=True, exist_ok=True)
        chosen_episode = query.episode or candidate.episode
        prefix = f"S{query.season:02d}E{chosen_episode:02d} - " if query.season and chosen_episode else ""
        stem = safe_name(Path(original).stem)[:80]
        filename = prefix + stem + suffix
        target = folder / filename
        index = 2
        while target.exists() or target.with_name(target.name + ".source.json").exists():
            target = folder / f"{prefix}{stem} ({index}){suffix}"
            index += 1
        # Exclusive creation keeps old subtitles even if another download races us.
        with target.open("xb") as handle:
            handle.write(data)
        meta = {"provider": candidate.provider, "source_url": candidate.source_url,
                "download_url": candidate.download_url, "original_filename": name,
                "title": query.title, "season": query.season, "episode": chosen_episode,
                "language": candidate.language, "note": note}
        target.with_name(target.name + ".source.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return target, note
