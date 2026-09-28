import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


FORMATS = {".srt", ".smi", ".sami", ".vtt", ".ass", ".ssa"}
ARCHIVES = {".zip", ".7z", ".rar"}


def normal(text: str) -> str:
    return "".join(c.lower() for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def season_of(text: str) -> int | None:
    s = unicodedata.normalize("NFKC", text)
    match = re.search(r"(?:\bseason\s*|시즌\s*|제\s*)(\d{1,2})\s*(?:기|期)?\b|\b(\d{1,2})\s*(?:기|期)\b", s, re.I)
    if match:
        return int(match.group(1) or match.group(2))
    match = re.search(r"\bS(\d{1,2})[ ._-]*E\d{1,3}\b", s, re.I)
    return int(match.group(1)) if match else None


def episode_of(text: str) -> int | None:
    text = unicodedata.normalize("NFKC", text)
    for pattern in (r"\bS\d{1,2}[ ._-]*E(\d{1,3})\b",
                    r"(?:^|[^\d])(?:제\s*)?(\d{1,3})\s*(?:화|話|회)(?:\D|$)",
                    r"(?:^|[\s._\[(#-])(?:EP?|#)\s*0*(\d{1,3})(?=[\s._\])#-]|$)"):
        found = re.search(pattern, text, re.I)
        if found:
            return int(found.group(1))
    return None


def episode_range(text: str) -> tuple[int, int] | None:
    found = re.search(r"(?:^|\D)(\d{1,3})\s*[~～〜\-–]\s*(\d{1,3})\s*(?:화|話|회|완결|자막|$)", text)
    if found:
        return int(found.group(1)), int(found.group(2))
    return None


def matching_post(post: str, aliases: list[str], season: int | None, episode: int | None) -> str:
    """Returns exact/review/none; preserve uncertain matches for manual review."""
    if not post or not aliases:
        return "none"
    if season and season_of(post) and season_of(post) != season:
        return "none"
    if episode:
        numbers = episode_range(post)
        if numbers and not numbers[0] <= episode <= numbers[1]:
            return "none"
        found = episode_of(post)
        if found is not None and found != episode and not numbers:
            return "none"
    prefix = re.split(r"(?:\d{1,3}\s*(?:화|話|회)|\bS\d{1,2}[ ._-]*E\d{1,3}\b|자막)", post, maxsplit=1, flags=re.I)[0]
    title = normal(prefix) or normal(post)
    names = [normal(name) for name in aliases if len(normal(name)) >= 2]
    if not any(name == title or (len(name) >= 4 and name in title) for name in names):
        return "none"
    if episode and episode_of(post) == episode and any(name == title for name in names):
        return "exact"
    return "review"


def suitable_file(name: str, episode: int | None) -> bool:
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if suffix not in FORMATS | ARCHIVES:
        return False
    if episode is None or suffix in ARCHIVES:
        return True
    number = episode_of(name)
    return number is None or number == episode


def drive_id(url: str) -> str | None:
    from urllib.parse import parse_qs
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname != "drive.google.com":
        return None
    found = re.match(r"/file/d/([\w-]+)(?:/|$)", u.path)
    ident = found.group(1) if found else parse_qs(u.query).get("id", [""])[0] if u.path in ("/open", "/uc") else ""
    return ident if re.fullmatch(r"[\w-]+", ident) else None


def file_name_from_url(url: str) -> str:
    return unquote(urlsplit(url).path.rstrip("/").split("/")[-1])
