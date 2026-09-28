"""Launch the desktop app, or use the searchable CLI for scripts/debugging."""

import argparse
import json
from pathlib import Path
import sys

from subfinder.downloader import Downloader
from subfinder.engine import SearchEngine
from subfinder.models import Query
from subfinder.network import HttpClient
from subfinder.providers.opensubtitles import OpenSubtitles
from subfinder.providers.reanime import ReAnime
from subfinder.providers.bigfile import Bigfile


def cli(args):
    folder = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        config = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    http = HttpClient()
    open_source = OpenSubtitles(http, config.get("opensubtitles_api_key", ""))
    engine = SearchEngine(ReAnime(http), open_source, Bigfile(http))
    query = Query(args.title, args.language, args.season, args.episode)
    result = (engine.search_open(query) if args.open_only else
              engine.search_bigfile(query) if args.bigfile_only else engine.search(query))
    for index, candidate in enumerate(result.candidates, 1):
        print(f"{index}. [{candidate.provider}] {candidate.title} / {candidate.file_name} / {candidate.confidence}")
    for warning in result.warnings:
        print("알림:", warning)
    if args.download:
        if not 1 <= args.download <= len(result.candidates):
            raise SystemExit("선택한 번호에 해당하는 자막이 없음")
        candidate = result.candidates[args.download - 1]
        if candidate.file_id is None and not candidate.download_url:
            raise SystemExit("이 출처는 사이트에서 로그인한 뒤 수동 다운로드해야 함: " + candidate.source_url)
        path, note = Downloader(folder / "subtitles", http, open_source).download(candidate, query)
        print("저장:", path)
        if note:
            print("알림:", note)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="애니 자막 찾기")
    parser.add_argument("title", nargs="?")
    parser.add_argument("--season", type=int)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--language", default="ko")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--open-only", action="store_true", help="OpenSubtitles만 검색")
    source.add_argument("--bigfile-only", action="store_true", help="Bigfile 애니 자막을 영문 제목으로 검색")
    parser.add_argument("--download", type=int, metavar="번호", help="검색 결과에서 파일 번호를 골라 저장")
    values = parser.parse_args()
    if values.title:
        cli(values)
    else:
        from subfinder.gui import main
        main()
