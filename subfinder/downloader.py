"""Download, archive selection, filename safety, and auditable output paths."""

from email.message import Message
from html import unescape
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile
from urllib.parse import parse_qs, unquote_to_bytes, urlencode, urlsplit
import zipfile
import zlib

from .matching import (ARCHIVES, FORMATS, drive_id, file_name_from_url,
                       filename_episode, is_special)
from .models import Candidate, Query
from .network import HttpClient, SourceError, allowed_url
from .providers.opensubtitles import OpenSubtitles


FILE_LIMIT = 6 * 1024 * 1024
SINGLE_LIMIT = 5 * 1024 * 1024


def _decode_filename_bytes(raw: bytes, original: str) -> str:
    """Recover UTF-8 or Korean legacy bytes without replacing valid characters."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = raw.decode("cp949")
        except UnicodeDecodeError:
            return original
        # CP949's extension maps accent + ASCII byte pairs to Hangul too.
        # Require a standard EUC-KR Hangul pair to avoid corrupting names such
        # as CP437 "école" while accepting mixed CP949 Korean filenames.
        for character in decoded:
            if "가" <= character <= "힣":
                pair = character.encode("cp949")
                if len(pair) == 2 and 0xB0 <= pair[0] <= 0xC8 and 0xA1 <= pair[1] <= 0xFE:
                    return decoded
        return original


def _readable_filename(value: str) -> str:
    # Some attachment servers percent-encode filename= rather than filename*=.
    if re.search(r"%[0-9a-fA-F]{2}", value):
        value = _decode_filename_bytes(unquote_to_bytes(value), value)
    # HTTP headers expose raw non-ASCII bytes as Latin-1 through urllib.
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        return value
    return _decode_filename_bytes(raw, value)


def _zip_filename(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    # Older Python versions ignore the Info-ZIP Unicode path extra field.
    # Honor it only when its CRC proves it belongs to this stored filename.
    extra = info.extra
    while len(extra) >= 4:
        tag, size = struct.unpack_from("<HH", extra)
        field, extra = extra[4:4 + size], extra[4 + size:]
        if len(field) != size:
            break
        if (tag == 0x7075 and len(field) >= 5 and field[0] == 1
                and struct.unpack_from("<I", field, 1)[0] == zlib.crc32(raw)):
            try:
                return field[5:].decode("utf-8")
            except UnicodeDecodeError:
                pass
    return _decode_filename_bytes(raw, info.filename)


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


def _zip_selection(data: bytes, episode: int | None, allow_special: bool = False,
                   require_episode: bool = False) -> tuple[bytes, str] | None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        items = archive.infolist()
        if len(items) > 256 or sum(info.file_size for info in items) > 64 * 1024 * 1024:
            raise SourceError("압축 파일의 항목/전체 크기가 제한을 초과함")
        choices = []
        for info in items:
            if info.is_dir() or info.file_size > SINGLE_LIMIT or info.flag_bits & 1:
                continue
            member_name = _zip_filename(info)
            name = PurePosixPath(member_name.replace("\\", "/")).name
            if (name and not name.startswith(".") and "__MACOSX" not in member_name
                    and Path(name).suffix.lower() in FORMATS
                    and (allow_special or not is_special(name))):
                choices.append((info, name))
        if episode is not None:
            exact = [(i, n) for i, n in choices if filename_episode(n) == episode]
            if exact:
                choices = exact
            elif require_episode or not (len(choices) == 1 and filename_episode(choices[0][1]) is None
                      and not any(c.isdigit() for c in Path(choices[0][1]).stem)):
                return None
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


def _seven_zip_selection(data: bytes, suffix: str, episode: int | None,
                         allow_special: bool = False, require_episode: bool = False) -> tuple[bytes, str] | None:
    binary = (shutil.which("7zz") or shutil.which("7z") or
              next((str(p) for p in (Path(r"C:\Program Files\7-Zip\7z.exe"),
                                    Path(r"C:\Program Files (x86)\7-Zip\7z.exe")) if p.exists()), None))
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="subtitle-archive-") as folder:
        path = Path(folder) / ("source" + suffix)
        path.write_bytes(data)
        listing = subprocess.run([binary, "l", "-slt", "-bd", "-sccUTF-8", str(path)], capture_output=True, timeout=20)
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
            if (size.isdecimal() and int(size) <= SINGLE_LIMIT and Path(base).suffix.lower() in FORMATS
                    and not base.startswith(".") and "__MACOSX" not in name
                    and (allow_special or not is_special(base))):
                choices.append((name, base))
        if len(choices) > 256:
            raise SourceError("압축 파일 항목 수가 제한을 초과함")
        if episode is not None:
            exact = [(name, base) for name, base in choices if filename_episode(base) == episode]
            if exact:
                choices = exact
            elif require_episode or not (len(choices) == 1 and filename_episode(choices[0][1]) is None
                      and not any(c.isdigit() for c in Path(choices[0][1]).stem)):
                return None
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
    values = [value for key, value in (msg.get_params(header="content-disposition") or [])
              if key.lower() == "filename"]
    # RFC 6266 gives filename* priority over the ASCII filename fallback.
    for value in sorted(values, key=lambda value: not isinstance(value, tuple)):
        if isinstance(value, tuple):
            charset, _, encoded = value
            try:
                value = encoded.encode("latin-1").decode(charset or "utf-8")
            except (LookupError, UnicodeError):
                continue
            if value:
                return value  # filename* has already been percent-decoded.
        if value:
            return _readable_filename(str(value))
    return ""


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
        # A browse-all search has no episode in its query; the selected result
        # still identifies the episode to check inside an archive.
        requested_episode = query.episode if query.episode is not None else candidate.episode
        if (candidate.source_episode is not None
                and (candidate.source_episode <= 0 or candidate.episode != requested_episode
                     or query.season not in (None, candidate.season))):
            raise SourceError("시즌별 화수와 원문 통산 화수 후보가 현재 요청과 다름. 다시 검색해 줘")
        source_episode = candidate.source_episode if candidate.source_episode is not None else requested_episode
        if candidate.file_id is not None:
            data, name = self.opensubtitles.download(candidate)
            name = _readable_filename(name)
        else:
            if not candidate.download_url or not allowed_url(candidate.download_url):
                raise SourceError("다운로드 링크가 허용되지 않음")
            response = (self._drive(candidate.download_url) if drive_id(candidate.download_url)
                        else self.http.get_bytes(candidate.download_url, FILE_LIMIT))
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise SourceError("자막 대신 웹페이지를 받음. 원문에서 공개 설정을 확인해 줘")
            data = response.body
            name = (_disposition_name(response.headers)
                    or _readable_filename(candidate.file_name or file_name_from_url(response.url)))
        if not data or len(data) > FILE_LIMIT:
            raise SourceError("빈 파일 또는 크기 제한 초과")
        suffix = _extension(data, name)
        note = ""
        if candidate.file_id is None and source_episode is not None and suffix not in ARCHIVES:
            file_episode = filename_episode(name)
            if file_episode is not None and file_episode != source_episode:
                raise SourceError("첨부 파일의 화 정보가 요청과 다름")
        allow_special = is_special(query.title)
        if suffix == ".zip":
            item = _zip_selection(data, source_episode, allow_special, candidate.require_episode)
            if item:
                data, name = item
                suffix = _extension(data, name)
            else:
                note = "압축 안에서 자막 한 개를 확정하지 못해 원본 ZIP 저장"
        elif suffix in (".7z", ".rar"):
            item = _seven_zip_selection(data, suffix, source_episode, allow_special,
                                        candidate.require_episode)
            if item:
                data, name = item
                suffix = _extension(data, name)
            else:
                note = "해당 화 자막을 한 개로 확정하지 못해 원본 압축 파일 저장 (7-Zip 설치 시 선택 지원)"
        if len(data) > SINGLE_LIMIT and suffix not in ARCHIVES:
            raise SourceError("자막 파일 크기가 제한을 초과함")
        original = safe_name(Path(name.replace("\\", "/")).name)
        base = safe_name(query.title, "Untitled")
        chosen_season = query.season if query.season is not None else (
            candidate.season if candidate.source_episode is not None else None)
        folder = self.output / base / (f"Season {chosen_season:02d}" if chosen_season else "Unsorted")
        folder.mkdir(parents=True, exist_ok=True)
        chosen_episode = requested_episode
        prefix = f"S{chosen_season:02d}E{chosen_episode:02d} - " if chosen_season and chosen_episode else ""
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
                "title": query.title, "season": chosen_season, "episode": chosen_episode,
                "language": candidate.language, "note": note}
        if candidate.source_episode is not None:
            meta["source_episode"] = candidate.source_episode
        target.with_name(target.name + ".source.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return target, note
