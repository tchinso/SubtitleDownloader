"""Small HTML extractor; sites are parsed as data, never evaluated as code."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re


@dataclass
class Link:
    href: str
    text: str
    download: str = ""
    heading: bool = False
    attributes: dict[str, str] = field(default_factory=dict)


class Document(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.meta: dict[str, str] = {}
        self.frames: list[str] = []
        self.title = ""
        self._anchor: dict | None = None
        self._clickable: dict | None = None
        self._title_depth = 0
        self._heading = False
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key:
                self.meta[key.lower()] = attrs.get("content", "")
        if tag == "iframe" and attrs.get("src"):
            self.frames.append(attrs["src"])
        if tag == "a":
            self._anchor = {"href": attrs.get("href", ""), "download": attrs.get("download", ""),
                            "text": [], "heading": False, "attributes": attrs}
        if tag == "li" and attrs.get("onclick"):
            # Some Tistory skins use a literal window.open('/entry/...') in
            # search cards instead of an anchor. Parse the URL as data.
            match = re.fullmatch(r"\s*window\.open\(\s*(['\"])(.*?)\1\s*,\s*(['\"])_self\3\s*\)\s*;?\s*",
                                 attrs["onclick"])
            if match:
                self._clickable = {"href": match.group(2), "text": [], "attributes": attrs}
        if tag == "h3" and self._anchor is not None:
            self._anchor["heading"] = True
        if tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            a = self._anchor
            self.links.append(Link(a["href"], re.sub(r"\s+", " ", "".join(a["text"])).strip(),
                                   a["download"], a["heading"], a["attributes"]))
            self._anchor = None
        if tag == "li" and self._clickable is not None:
            item = self._clickable
            self.links.append(Link(item["href"], re.sub(r"\s+", " ", "".join(item["text"])).strip(),
                                   attributes=item["attributes"]))
            self._clickable = None
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"].append(data)
        if self._clickable is not None:
            self._clickable["text"].append(data)
        if self._title_depth:
            self.title += data
