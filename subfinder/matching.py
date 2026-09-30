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
    match = re.search(r"(?:\b(?:season|시즌)\s*|[제第]\s*)?(\d{1,2})\s*(?:기|期)(?!\w)|\b(?:season|시즌)\s*(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", s, re.I)
    if match:
        return int(next(value for value in match.groups() if value))
    match = re.search(r"\bS(\d{1,2})[ ._-]*E\d{1,3}\b", s, re.I)
    if match:
        return int(match.group(1))
    if not re.search(r"\b(?:Part|Cour)\s*\d+\s*$", s, re.I):
        match = re.search(r"(?:^|\s)(\d{1,2})(?:st|nd|rd|th)\s*$", s, re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"(?:^|\s|(?<=[가-힣ぁ-んァ-ヶ一-龯]))(VIII|VII|III|VI|IV|IX|II|V|X)(?=\s*(?:$|[:~～〜]|[-–—]\s|Part\b|Cour\b))", s)
        if match:
            return {"II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
                    "VII": 7, "VIII": 8, "IX": 9, "X": 10}.get(match.group(1))
        match = re.search(r"(?:^|\s)([2-9])\s*$", s)
        if match:
            return int(match.group(1))
    return None


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


def without_mixed_specials(name: str) -> str:
    """Treat a TV + OVA bundle as TV; individual OVA files remain special."""
    return re.sub(r"\bTV\s*\+\s*(?:OVA|OAD|SP)(?:\s*\+\s*(?:OVA|OAD|SP))*(?=$|[^a-z])",
                  "TV", name, flags=re.I)


def is_special(name: str) -> bool:
    return bool(re.search(r"(?:^|[^a-z])(?:OVA|OAD|SP|SPECIAL|CHRISTMAS|NCOP|NCED)(?=$|[^a-z])|특전|외전|특별편",
                          name, re.I))


def filename_episode(name: str) -> int | float | None:
    """Find an episode marker in a subtitle filename, including bare final numbers."""
    stem = PurePosixPath(name.replace("\\", "/")).name
    stem = re.sub(r"\.[^.]+$", "", unicodedata.normalize("NFKC", stem))
    stem = re.sub(r"(?:\s*\[[^\]]*\]|\s*\([^)]*\))+\s*$", "", stem).strip()
    stem = re.sub(r"((?:^|[\s._-])\d+(?:\.\d+)?(?:화|話|회))\s*[-_]\s*[a-z][a-z0-9._-]*$", r"\1", stem, flags=re.I)
    stem = re.sub(r"(\d)[\s._-]+(?:TV|BD|WEB)$", r"\1", stem, flags=re.I)
    stem = re.sub(r"(\d)\s+END$", r"\1", stem, flags=re.I)
    stem = re.sub(r"(\d)[\s._-]*(?:SubsPlease|Ohys(?:-Raws)?|NanDesuKa)$", r"\1", stem, flags=re.I)
    number = r"(\d+(?:\.\d+)?)"
    for pattern in (rf"S\d+E{number}",
                    rf"(?:^|[\s._-])#\s*0*{number}(?:\s+END)?$",
                    rf"(?:^|[\s._-])(?:EP?|제)\s*0*{number}(?:화|話|회|\b)",
                    rf"(?:^|[\s._-])0*{number}(?:화|話|회)?(?:\s*\([^)]*\))?$"):
        found = re.search(pattern, stem, re.I)
        if found:
            value = float(found.group(1))
            return int(value) if value.is_integer() else value
    return None


def _has_title_identity(text: str) -> bool:
    """Season, sequel, and special markers must survive title shortening."""
    return bool(season_of(text) or is_special(text) or re.search(
        r"\b(?:[A-Z]|[IVX]{2,4}|Part|Cour)\b|극장판", text, re.I))


def title_queries(subject: str, aliases=()) -> list[str]:
    """Search full title, a safe subtitle omission, and confirmed Korean aliases."""
    titles = [subject, *(alias for alias in aliases if isinstance(alias, str) and re.search(r"[가-힣]", alias))]

    def shortened(title: str) -> str | None:
        match = (re.match(r"^(.*?)\s+[~～〜]+[^~～〜]+[~～〜]+\s*$", title)
                 or re.match(r"^(.*?)\s+(?:[-–—]|:)\s+.+$", title))
        if not match or len(normal(match.group(1))) < 3:
            return None
        removed = unicodedata.normalize("NFKC", title[len(match.group(1)):]).replace("~", " ").replace("～", " ").replace("〜", " ")
        if _has_title_identity(removed):
            return None
        return match.group(1).strip()

    queries = [subject, shortened(subject)]
    for alias in titles[1:]:
        queries.extend((alias, shortened(alias)))
    return list(dict.fromkeys(query for query in queries if query))[:3]


def blog_title_alias(post: str, aliases: list[str], season: int | None = None) -> str | None:
    """Confirm a shorter title using the work's registered creator post.

    Only a complete post title that is a prefix of a known title qualifies;
    arbitrary shared words and partial words do not establish a work's identity.
    """
    title = unicodedata.normalize("NFKC", post)
    title = re.sub(r"^\s*\[(?:자막|Erai-raws|Ohys-Raws|SubsPlease|Moozzi2|Snow-Raws)\]\s*",
                   "", title, flags=re.I)
    title = re.split(
        r"(?:\d{1,3}\s*[~～〜\-–]\s*)?\d{1,3}\s*(?:화|話|회)"
        r"|\bS\d{1,2}[ ._-]*E\d{1,3}\b|자막", title, maxsplit=1, flags=re.I)[0]
    title = _without_known_title_notes(title, aliases).strip()
    title = re.sub(r"\s*[\[(](?:完|완결|END)[\])]\s*$", "", title, flags=re.I)
    title = re.sub(r"(?:\s+(?:BD|TV|블루레이|전체|통합|완결|한글|한국어))+$", "", title, flags=re.I)
    title = title.strip(" \t\r\n-–—:~～〜")
    normalized = normal(title)
    if len(normalized) < 4 or (season and season > 1 and season_of(title) != season):
        return None
    for alias in aliases:
        known = unicodedata.normalize("NFKC", alias)
        value = normal(known)
        if not value.startswith(normalized) or value == normalized:
            continue
        positions = [index for index, character in enumerate(known) if character.isalnum()]
        remainder = known[positions[len(normalized) - 1] + 1:]
        if remainder[:1].isalnum() or _has_title_identity(remainder):
            continue
        if season_of(title) != season_of(known) or is_special(title) != is_special(known):
            continue
        return title
    return None


def _without_known_title_notes(title: str, aliases: list[str]) -> str:
    known_titles = sorted({normal(alias) for alias in aliases if len(normal(alias)) >= 3}, key=len, reverse=True)

    def remove(match):
        note = normal(match.group(1))
        matched = False
        for known in known_titles:
            if known in note:
                note = note.replace(known, "")
                matched = True
        return " " if matched and (not note or re.fullmatch(r"(?:19|20)\d{2}", note)) else match.group(0)

    return re.sub(r"\(([^()]*)\)", remove, title)


def matching_post(post: str, aliases: list[str], season: int | None, episode: int | None) -> str:
    """Returns exact/review/none; preserve uncertain matches for manual review."""
    if not post or not aliases:
        return "none"
    post = unicodedata.normalize("NFKC", post)
    post = re.sub(r"^\s*\[(?:자막|Erai-raws|Ohys-Raws|SubsPlease|Moozzi2|Snow-Raws)\]\s*", "", post, flags=re.I)
    if not any(is_special(alias) for alias in aliases) and is_special(without_mixed_specials(post)):
        return "none"
    prefix = re.split(r"(?:\d{1,3}\s*(?:화|話|회)|\bS\d{1,2}[ ._-]*E\d{1,3}\b|자막)", post, maxsplit=1, flags=re.I)[0]
    mixed_seasons = re.search(r"\s+(\d+\s*기(?:\s*/\s*\d+\s*기)+)\s*$", prefix)
    if mixed_seasons and season and season in [int(number) for number in re.findall(r"\d+", mixed_seasons.group(1))]:
        prefix = prefix[:mixed_seasons.start()] + f" {season}기"
        post_season = season
    else:
        post_season = season_of(post)
    if season and post_season and post_season != season:
        return "none"
    if episode:
        numbers = episode_range(post)
        if numbers and not numbers[0] <= episode <= numbers[1]:
            return "none"
        found = episode_of(post)
        if found is not None and found != episode and not numbers:
            return "none"
        if found is None and numbers is None:
            return "none"
    title = normal(_without_known_title_notes(prefix, aliases)) or normal(post)
    names = [normal(name) for name in aliases if len(normal(name)) >= 2]
    if not any(name == title or (len(name) >= 4 and name in title) for name in names):
        return "none"
    if episode and episode_of(post) == episode and any(name == title for name in names):
        return "exact"
    return "review"


def matching_public_title(post: str, aliases: list[str], season: int | None,
                          episode: int | None) -> str:
    """Match a public aggregate post whose title may omit an episode number."""
    explicit = matching_post(post, aliases, season, episode)
    if explicit != "none":
        return explicit
    if not post or not aliases or episode_of(post) is not None or episode_range(post) is not None:
        return "none"
    title = unicodedata.normalize("NFKC", without_mixed_specials(post))
    if not is_special(aliases[0]) and is_special(title):
        return "none"
    title = re.sub(r"^\s*\[(?:자막|Erai-raws|Ohys-Raws|SubsPlease|Moozzi2|Snow-Raws)\]\s*", "", title, flags=re.I)
    title = re.sub(r"[\[(](?:BD(?:Rip)?|WEB(?:Rip)?|DVD)?\s*(?:\d{3,4}p|\d{3,4}x\d{3,4})(?:\s+(?:x\.?26[45]|h\.?26[45]|FLAC|AAC|10bit|8bit))*[\])]", "", title, flags=re.I)
    title = title.split("자막", 1)[0]
    title = re.sub(r"(?:[\s-]*(?:BD|TV|블루레이|전체|통합|완결|한글|한국어)\s*)+$", "", title, flags=re.I).strip()
    title = _without_known_title_notes(title, aliases)
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return "none"
    target_season = season or season_of(aliases[0]) or 1
    if (season_of(title) or 1) != target_season:
        return "none"
    normalized = normal(title)
    if normalized == normal(aliases[0]):
        return "exact"
    return "review" if any(normalized == normal(alias) for alias in aliases[1:]) else "none"


def suitable_file(name: str, episode: int | None) -> bool:
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if suffix not in FORMATS | ARCHIVES:
        return False
    if episode is None or suffix in ARCHIVES:
        return True
    number = filename_episode(name)
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
