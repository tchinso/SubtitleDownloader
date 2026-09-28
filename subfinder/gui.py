"""Tkinter front end; network operations run in worker threads."""

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from .bridge import BridgeServer, PORT
from .downloader import Downloader
from .engine import SearchEngine
from .models import AnimeSearchResult, Query, SearchResult
from .network import HttpClient
from .providers.opensubtitles import OpenSubtitles
from .providers.reanime import ReAnime


def app_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.folder = app_folder()
        self.config_file = self.folder / "settings.json"
        try:
            self.config = json.loads(self.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.config = {}
        self.config.setdefault("edge_token", secrets.token_urlsafe(24))
        self.save_settings()
        self.http = HttpClient()
        self.reanime = ReAnime(self.http)
        self.open = OpenSubtitles(self.http, self.config.get("opensubtitles_api_key", ""))
        self.engine = SearchEngine(self.reanime, self.open)
        self.downloader = Downloader(self.folder / "subtitles", self.http, self.open)
        self.results = SearchResult()
        self.anime_results = AnimeSearchResult()
        self.current_query: Query | None = None
        self.busy = False
        self.generation = 0
        self.events: Queue = Queue()
        self.tab_inbox: Queue = Queue()
        self.bridge = None
        self.root.title("애니 자막 찾기 · ReAnime 우선")
        self.root.geometry("1100x790")
        self._widgets()
        self._start_bridge()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def save_settings(self):
        self.config_file.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _widgets(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="애니 제목").grid(row=0, column=0, sticky="w")
        self.title = tk.StringVar()
        self.title_entry = ttk.Entry(top, textvariable=self.title)
        self.title_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=6)
        self.title_entry.bind("<Return>", lambda _: self.search())
        ttk.Label(top, text="시즌").grid(row=1, column=0, sticky="w", pady=8)
        self.season = tk.StringVar()
        ttk.Entry(top, width=8, textvariable=self.season).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(top, text="화수").grid(row=1, column=2, sticky="e")
        self.episode = tk.StringVar()
        ttk.Entry(top, width=8, textvariable=self.episode).grid(row=1, column=3, sticky="w", padx=6)
        ttk.Label(top, text="언어").grid(row=1, column=4, sticky="e")
        self.language = tk.StringVar(value="ko")
        ttk.Combobox(top, textvariable=self.language, width=9, values=("ko", "ja", "en", "zh", "es", "fr", "de"), state="readonly").grid(row=1, column=5, sticky="w", padx=6)
        top.columnconfigure(1, weight=1)
        actions = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        actions.pack(fill="x")
        self.search_button = ttk.Button(actions, text="작품 찾기", command=self.search)
        self.search_button.pack(side="left", padx=(0, 6))
        self.title_search_button = ttk.Button(actions, text="입력 제목으로 자막 검색", command=self.search_title)
        self.title_search_button.pack(side="left", padx=6)
        self.open_button = ttk.Button(actions, text="OpenSubtitles에서도 검색", command=self.search_open)
        self.open_button.pack(side="left", padx=6)
        self.download_button = ttk.Button(actions, text="선택 자막 다운로드", command=self.download)
        self.download_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(actions, text="취소", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        ttk.Button(actions, text="저장 폴더", command=self.open_folder).pack(side="right", padx=6)
        ttk.Button(actions, text="OpenSubtitles API 키", command=self.configure_key).pack(side="right", padx=6)
        self.status = tk.StringVar(value="키워드로 작품을 찾은 뒤 작품을 선택해 자막을 검색해 줘")
        ttk.Label(self.root, textvariable=self.status, padding=(12, 0, 12, 7)).pack(anchor="w")

        anime_bar = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        anime_bar.pack(fill="x")
        ttk.Label(anime_bar, text="검색된 작품").pack(side="left")
        self.anime_search_button = ttk.Button(anime_bar, text="선택 작품 자막 검색", command=self.search_selected,
                                              state="disabled")
        self.anime_search_button.pack(side="left", padx=10)
        anime_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        anime_frame.pack(fill="x")
        self.anime_tree = ttk.Treeview(anime_frame, columns=("subject", "original"), show="headings",
                                       selectmode="browse", height=6)
        self.anime_tree.heading("subject", text="작품명")
        self.anime_tree.heading("original", text="원제")
        self.anime_tree.column("subject", width=480)
        self.anime_tree.column("original", width=480)
        self.anime_tree.pack(side="left", fill="x", expand=True)
        anime_scrollbar = ttk.Scrollbar(anime_frame, orient="vertical", command=self.anime_tree.yview)
        anime_scrollbar.pack(side="right", fill="y")
        self.anime_tree.configure(yscrollcommand=anime_scrollbar.set)
        self.anime_tree.bind("<<TreeviewSelect>>", self._select_anime)
        self.anime_tree.bind("<Double-1>", lambda _: self.search_selected())
        self.anime_tree.bind("<Return>", lambda _: self.search_selected())

        ttk.Label(self.root, text="자막 후보", padding=(12, 0, 12, 0)).pack(anchor="w")
        columns = ("source", "title", "episode", "lang", "file", "match")
        frame = ttk.Frame(self.root, padding=(12, 0, 12, 5))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (("source", "출처", 130), ("title", "작품 / 글", 250), ("episode", "시즌 / 화", 85),
                                  ("lang", "언어", 50), ("file", "파일명", 280), ("match", "일치", 70)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key in ("title", "file"))
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.details = tk.StringVar(value="자막 후보를 선택하면 원문 주소를 볼 수 있음")
        ttk.Label(self.root, textvariable=self.details, wraplength=970, padding=(12, 4, 12, 8)).pack(anchor="w")
        link = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        link.pack(fill="x")
        ttk.Label(link, text="게시글·파일 링크").pack(side="left")
        self.link = tk.StringVar()
        ttk.Entry(link, textvariable=self.link).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(link, text="이 링크에서 찾기", command=self.search_link).pack(side="left")
        ttk.Button(link, text="Google 직접 열기", command=self.google_open).pack(side="left", padx=(8, 0))
        footer = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        footer.pack(fill="x")
        ttk.Label(footer, text="엣지 연동 코드 (선택):").pack(side="left")
        code = ttk.Entry(footer, width=37)
        code.insert(0, self.config["edge_token"])
        code.configure(state="readonly")
        code.pack(side="left", padx=8)
        self.bridge_label = tk.StringVar(value="엣지 연동 시작 중")
        ttk.Label(footer, textvariable=self.bridge_label).pack(side="left")

    def _start_bridge(self):
        try:
            self.bridge = BridgeServer(self.config["edge_token"], self.tab_inbox)
            self.bridge.start()
            self.bridge_label.set(f"로컬 연결 대기: 127.0.0.1:{PORT}")
        except OSError as exc:
            self.bridge_label.set(f"엣지 연동 꺼짐: {exc}")

    def _query(self) -> Query | None:
        title = self.title.get().strip()
        if not title:
            messagebox.showinfo("작품명", "애니 제목을 먼저 입력해 줘")
            return None
        fields = []
        for label, variable in (("시즌", self.season), ("화수", self.episode)):
            value = variable.get().strip()
            if value and (not value.isdecimal() or not 1 <= int(value) <= 999):
                messagebox.showerror("입력 오류", f"{label}는 1~999 숫자로 입력해 줘")
                return None
            fields.append(int(value) if value else None)
        if self.language.get() != "ko" and not self.config.get("opensubtitles_api_key"):
            self.status.set("ReAnime 출처는 한국어 중심임. 다른 언어는 API 키를 등록하고 OpenSubtitles에서 검색해 줘")
        return Query(title=title, language=self.language.get(), season=fields[0], episode=fields[1])

    def _run(self, message: str, task):
        if self.busy:
            return
        self.busy = True
        self.generation += 1
        generation = self.generation
        self.status.set(message)
        self.search_button.configure(state="disabled")
        self.title_search_button.configure(state="disabled")
        self.anime_search_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.download_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        def worker():
            try:
                self.events.put((generation, "done", task()))
            except Exception as exc:
                self.events.put((generation, "error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def search(self):
        if self.busy:
            return
        query = self._query()
        if not query:
            return
        if query.language != "ko":
            self.current_query = query
            self._run("OpenSubtitles 검색 중…", lambda: ("search", self.engine.search(query)))
            return
        self._run("애니시아 작품 검색 중…", lambda: ("anime", self.reanime.discover(query.title)))

    def search_title(self):
        if self.busy:
            return
        query = self._query()
        if not query:
            return
        self.current_query = query
        self._run("애니시아와 자막 제작자 글 검색 중…", lambda: ("search", self.engine.search(query)))

    def search_selected(self):
        if self.busy:
            return
        selected = self.anime_tree.selection()
        if not selected:
            return
        anime = next((a for a in self.anime_results.anime if str(a.anime_no) == selected[0]), None)
        if anime is None:
            return
        query = self._query()
        if query is None:
            return
        query = Query(anime.subject, query.language, query.season, query.episode)
        self.title.set(anime.subject)
        self.current_query = query
        self._run(f"{anime.subject} 자막 검색 중…", lambda: ("search", self.engine.search_selected(query, anime)))

    def search_open(self):
        if self.busy:
            return
        query = self._query()
        if not query:
            return
        existing = self.results if query == self.current_query else SearchResult()
        self.current_query = query
        self._run("OpenSubtitles 검색 중…", lambda: ("search", self.engine.search_open(query, existing)))

    def search_link(self):
        if self.busy:
            return
        query = self._query()
        if not query:
            return
        url = self.link.get().strip()
        self.current_query = query
        self._run("입력한 출처 확인 중…", lambda: ("search", self.reanime.direct(url, query)))

    def download(self):
        if self.busy:
            return
        if not self.current_query:
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("자막 후보", "먼저 자막 파일을 선택해 줘")
            return
        candidate = self.results.candidates[int(selected[0])]
        query = self.current_query
        self._run("선택한 자막 다운로드 중…", lambda: ("download", self.downloader.download(candidate, query)))

    def cancel(self):
        if not self.busy:
            return
        self.generation += 1
        self.busy = False
        self.search_button.configure(state="normal")
        self.title_search_button.configure(state="normal")
        self.anime_search_button.configure(state="normal" if self.anime_tree.selection() else "disabled")
        self.open_button.configure(state="normal")
        self.download_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status.set("화면 대기를 취소함. 진행 중인 HTTPS 요청은 제한 시간 후 종료됨")

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        self.details.set("자막 후보를 선택하면 원문 주소를 볼 수 있음")
        for index, candidate in enumerate(self.results.candidates):
            target = f"S{candidate.season or '?'}E{candidate.episode or '?'}"
            self.tree.insert("", "end", iid=str(index), values=(candidate.provider, candidate.title, target,
                                                                 candidate.language, candidate.file_name, candidate.confidence))

    def _populate_anime(self):
        self.anime_tree.delete(*self.anime_tree.get_children())
        for anime in self.anime_results.anime:
            self.anime_tree.insert("", "end", iid=str(anime.anime_no),
                                   values=(anime.subject, anime.original_subject))
        self.anime_search_button.configure(state="disabled")

    def _select_anime(self, _event=None):
        self.anime_search_button.configure(state="normal" if self.anime_tree.selection() and not self.busy else "disabled")

    def _select(self, _event=None):
        selected = self.tree.selection()
        if selected:
            candidate = self.results.candidates[int(selected[0])]
            self.details.set(f"원문: {candidate.source_url}  |  제작자: {candidate.creator or '-'}  |  {candidate.note}")

    def _poll(self):
        try:
            while True:
                generation, kind, value = self.events.get_nowait()
                if generation != self.generation:
                    continue
                self.busy = False
                self.search_button.configure(state="normal")
                self.title_search_button.configure(state="normal")
                self.anime_search_button.configure(state="normal" if self.anime_tree.selection() else "disabled")
                self.open_button.configure(state="normal")
                self.download_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if kind == "error":
                    self.status.set("작업 실패: " + value)
                    continue
                operation, outcome = value
                if operation == "download":
                    path, note = outcome
                    self.status.set("저장됨: " + str(path) + (" · " + note if note else ""))
                elif operation == "anime":
                    self.anime_results = outcome
                    self._populate_anime()
                    self.results = SearchResult()
                    self.current_query = None
                    self._populate()
                    warning = " · " + outcome.warnings[0] if outcome.warnings else ""
                    self.status.set(f"작품 {len(outcome.anime)}개 · 작품을 선택해 자막 검색{warning}" if outcome.anime else
                                    f"검색된 작품 없음 · 정확한 제목이면 입력 제목으로 자막 검색{warning}")
                else:
                    self.results = outcome
                    self._populate()
                    suffix = " · OpenSubtitles 자동 검색" if outcome.fallback_used else ""
                    important = next((item for item in outcome.warnings if item.startswith(
                        ("OpenSubtitles", "Google 공개 검색", "애니시아 작품 검색", "애니시아 작품 목록"))), None)
                    warning = (" · " + (important or outcome.warnings[0])) if outcome.warnings else ""
                    self.status.set(f"후보 {len(outcome.candidates)}개{suffix}{warning}")
        except Empty:
            pass
        try:
            while True:
                self._from_edge(self.tab_inbox.get_nowait())
        except Empty:
            pass
        self.root.after(120, self._poll)

    def _from_edge(self, tab: dict):
        url = tab["url"]
        parsed = urlsplit(url)
        match = re.fullmatch(r"/watch/([a-z0-9-]+)/?", parsed.path) if parsed.hostname == "reanime.to" else None
        if match:
            self.title.set(match.group(1).replace("-", " "))
            ep = parse_qs(parsed.query).get("ep", [""])[0]
            if ep.isdecimal():
                self.episode.set(ep)
            self.status.set("엣지에서 작품 주소를 받음. 제목을 확인하고 검색해 줘")
        elif parsed.scheme == "https" and (parsed.hostname in ("blog.naver.com", "m.blog.naver.com", "drive.google.com")
                                            or (parsed.hostname or "").endswith((".tistory.com", ".blogspot.com"))):
            self.link.set(url)
            self.status.set("엣지에서 게시글 주소를 받음. 작품명을 적고 '이 링크에서 찾기'를 눌러 줘")
        else:
            guess = re.split(r"\s+[|–—]\s+", tab["title"], 1)[0].strip()
            if guess:
                self.title.set(guess)
            self.status.set("엣지에서 페이지 제목을 받음. 작품명을 확인하고 검색해 줘")

    def configure_key(self):
        value = simpledialog.askstring("OpenSubtitles API 키", "본인 OpenSubtitles API 키를 입력해 줘 (빈칸은 기존 키 삭제)",
                                       parent=self.root, show="*")
        if value is None:
            return
        self.config["opensubtitles_api_key"] = value.strip()
        self.open.api_key = value.strip()
        self.save_settings()
        self.status.set("OpenSubtitles API 키 설정 저장됨" if value.strip() else "OpenSubtitles API 키 삭제됨")

    def open_folder(self):
        folder = self.folder / "subtitles"
        folder.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder))
        else:
            self.status.set(str(folder))

    def google_open(self):
        from urllib.parse import urlencode
        query = self._query()
        if query:
            words = f"{query.title} {str(query.episode) + '화 ' if query.episode else ''}자막"
            webbrowser.open("https://www.google.com/search?" + urlencode({"hl": "ko", "q": words}))

    def close(self):
        if self.bridge:
            threading.Thread(target=self.bridge.stop, daemon=True).start()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
