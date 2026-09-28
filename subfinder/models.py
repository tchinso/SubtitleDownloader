from dataclasses import dataclass, field


@dataclass
class Query:
    title: str
    language: str = "ko"
    season: int | None = None
    episode: int | None = None
    slug: str = ""


@dataclass
class Candidate:
    provider: str
    title: str
    file_name: str
    source_url: str
    download_url: str = ""
    language: str = "ko"
    season: int | None = None
    episode: int | None = None
    confidence: str = "review"
    creator: str = ""
    note: str = ""
    file_id: int | None = None
    page_title: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.file_id or self.download_url}:{self.file_name}"


@dataclass
class SearchResult:
    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "empty"  # found, empty, error, review
    fallback_used: bool = False

    def add(self, other: "SearchResult") -> None:
        keys = {c.key for c in self.candidates}
        for candidate in other.candidates:
            if candidate.key not in keys:
                self.candidates.append(candidate)
                keys.add(candidate.key)
        self.warnings.extend(other.warnings)
        self.status = "found" if self.candidates else other.status
