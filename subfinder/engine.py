from .models import Anime, Query, SearchResult
from .providers.reanime import ReAnime
from .providers.opensubtitles import OpenSubtitles


class SearchEngine:
    def __init__(self, reanime: ReAnime, open_subtitles: OpenSubtitles):
        self.reanime = reanime
        self.open_subtitles = open_subtitles

    def search(self, query: Query) -> SearchResult:
        """Search Korean creator sources; OpenSubtitles is an explicit choice."""
        if query.language != "ko":
            return self.open_subtitles.search(query)
        return self.reanime.search(query)

    def search_selected(self, query: Query, anime: Anime) -> SearchResult:
        """Search the chosen Anissia work without guessing its identity again."""
        if query.language != "ko":
            return self.search(query)
        return self.reanime.search_selected(query, anime)

    def search_open(self, query: Query, existing: SearchResult | None = None) -> SearchResult:
        # The GUI searches in a worker thread. Do not mutate the list currently
        # being rendered if the user cancels and starts another search.
        result = (SearchResult(list(existing.candidates), list(existing.warnings),
                               existing.status)
                  if existing is not None else SearchResult())
        result.add(self.open_subtitles.search(query))
        return result
