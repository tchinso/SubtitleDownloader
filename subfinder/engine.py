from .models import Query, SearchResult
from .providers.reanime import ReAnime
from .providers.opensubtitles import OpenSubtitles


class SearchEngine:
    def __init__(self, reanime: ReAnime, open_subtitles: OpenSubtitles):
        self.reanime = reanime
        self.open_subtitles = open_subtitles

    def search(self, query: Query) -> SearchResult:
        """ReAnime first; only a completed empty result triggers API fallback."""
        if query.language != "ko":
            result = self.open_subtitles.search(query)
            result.fallback_used = True
            return result
        first = self.reanime.search(query)
        if first.candidates or first.status == "error":
            return first
        second = self.open_subtitles.search(query)
        second.fallback_used = True
        second.warnings = first.warnings + second.warnings
        return second

    def search_open(self, query: Query, existing: SearchResult | None = None) -> SearchResult:
        # The GUI searches in a worker thread. Do not mutate the list currently
        # being rendered if the user cancels and starts another search.
        result = (SearchResult(list(existing.candidates), list(existing.warnings),
                               existing.status, existing.fallback_used)
                  if existing is not None else SearchResult())
        result.add(self.open_subtitles.search(query))
        return result
