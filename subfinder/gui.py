"""Tkinter front end; network operations run in worker threads."""

import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
import webbrowser

from .downloader import Downloader
from .engine import SearchEngine
from .models import AnimeSearchResult, CreatorBlog, Query, SearchResult
from .network import HttpClient
from .providers.opensubtitles import OpenSubtitles
from .providers.reanime import ReAnime
from .providers.bigfile import Bigfile


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
        self.http = HttpClient()
        self.reanime = ReAnime(self.http)
        self.open = OpenSubtitles(self.http, self.config.get("opensubtitles_api_key", ""))
        self.bigfile = Bigfile(self.http)
        self.engine = SearchEngine(self.reanime, self.open, self.bigfile)
        self.downloader = Downloader(self.folder / "subtitles", self.http, self.open)
        self.results = SearchResult()
        self.anime_results = AnimeSearchResult()
        self.current_query: Query | None = None
        self.busy = False
        self.generation = 0
        self.events: Queue = Queue()
        self.root.title("애니 자막 찾기")
        self.root.geometry("1100x790")
        self._widgets()
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
        self.title_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6)
        self.title_entry.bind("<Return>", lambda _: self.search())
        ttk.Label(top, text="영문 제목").grid(row=1, column=0, sticky="w", pady=8)
        self.english_title = tk.StringVar()
        self.english_title_entry = ttk.Entry(top, textvariable=self.english_title)
        self.english_title_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=6)
        self.english_title_placeholder = ttk.Label(
            self.english_title_entry,
            text="애니시아 검색 시 필요없음, 가급적 로마자 표기 우선 예: 귀멸의 칼날→kimetsu no yaiba",
            foreground="#888888",
            background=ttk.Style(self.root).lookup("TEntry", "fieldbackground") or "white",
        )
        self.english_title_placeholder.bind("<Button-1>", lambda _: self.english_title_entry.focus_set())
        self.english_title.trace_add("write", self._update_english_title_placeholder)
        self._update_english_title_placeholder()
        language_frame = ttk.Frame(top)
        language_frame.grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Label(language_frame, text="OpenSubtitles 언어").pack(side="left")
        self.language = tk.StringVar(value="ko")
        ttk.Combobox(language_frame, textvariable=self.language, width=9, values=("ko", "ja", "en", "zh", "es", "fr", "de"), state="readonly").pack(side="left", padx=6)
        top.columnconfigure(1, weight=1)
        actions = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        actions.pack(fill="x")
        self.search_button = ttk.Button(actions, text="애니시아 작품 찾기", command=self.search)
        self.search_button.pack(side="left", padx=(0, 6))
        self.bigfile_button = ttk.Button(actions, text="Bigfile 검색 (영문)", command=self.search_bigfile)
        self.bigfile_button.pack(side="left", padx=6)
        self.open_button = ttk.Button(actions, text="OpenSubtitles 검색 (영문)", command=self.search_open)
        self.open_button.pack(side="left", padx=6)
        self.download_button = ttk.Button(actions, text="선택 자막 다운로드", command=self.download)
        self.download_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(actions, text="취소", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        ttk.Button(actions, text="저장 폴더", command=self.open_folder).pack(side="right", padx=6)
        ttk.Button(actions, text="OpenSubtitles API 키", command=self.configure_key).pack(side="right", padx=6)
        ttk.Button(actions, text="API 키 발급 방법", command=self.show_key_help).pack(side="right", padx=6)
        self.status = tk.StringVar(value="키워드로 작품을 찾은 뒤 작품을 선택해 자막을 검색해 줘")
        ttk.Label(self.root, textvariable=self.status, padding=(12, 0, 12, 7)).pack(anchor="w")

        anime_bar = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        self.anime_bar = anime_bar
        anime_bar.pack(fill="x")
        ttk.Label(anime_bar, text="검색된 작품").pack(side="left")
        self.title_search_button = ttk.Button(anime_bar, text="입력 제목으로 애니시아 자막 검색", command=self.search_title)
        self.title_search_button.pack(side="left", padx=10)
        self.anime_search_button = ttk.Button(anime_bar, text="선택 작품 자막 검색", command=self.search_selected,
                                              state="disabled")
        self.anime_search_button.pack(side="left")
        self.creator_blog_button = ttk.Button(anime_bar, text="선택 작품 원본 블로그 열기",
                                              command=self.open_creator_blog, state="disabled")
        self.creator_blog_button.pack(side="left")
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

        ttk.Label(self.root, text="자막 후보 (더블클릭 또는 우클릭으로 다운로드)", padding=(12, 0, 12, 0)).pack(anchor="w")
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
        self.tree.bind("<Double-1>", self._download_on_double_click)
        self.tree.bind("<Return>", lambda _: self.download())
        self.tree.bind("<Button-3>", self._candidate_context_menu)
        self.candidate_menu = tk.Menu(self.root, tearoff=False)
        self.candidate_menu.add_command(label="자막 다운로드", command=self.download)
        self.candidate_menu.add_command(label="원문 열기", command=self.open_source)
        self.details = tk.StringVar(value="자막 후보를 선택하면 원문 주소를 볼 수 있음")
        ttk.Label(self.root, textvariable=self.details, wraplength=970, padding=(12, 4, 12, 8)).pack(anchor="w")
        link = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        link.pack(fill="x")
        ttk.Label(link, text="게시글·파일 링크").pack(side="left")
        self.link = tk.StringVar()
        ttk.Entry(link, textvariable=self.link).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(link, text="이 링크에서 찾기", command=self.search_link).pack(side="left")
        ttk.Button(link, text="Google 직접 열기", command=self.google_open).pack(side="left", padx=(8, 0))

        other_sources = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        other_sources.pack(fill="x")
        ttk.Button(other_sources, text="선택 원문 열기", command=self.open_source).pack(side="left")
        ttk.Label(other_sources, text="Bigfile 자막은 로그인 없이 검색할 수 있으며, 다운로드에는 로그인이 필요합니다.").pack(side="left", padx=12)

    def _update_english_title_placeholder(self, *_args):
        if self.english_title.get():
            self.english_title_placeholder.place_forget()
        else:
            self.english_title_placeholder.place(x=5, rely=0.5, anchor="w")

    def _english_query(self, language_override: str | None = None) -> Query | None:
        title = self.english_title.get().strip()
        if not title:
            messagebox.showinfo("영문 제목", "Bigfile·OpenSubtitles 검색에 사용할 영문 또는 로마자 제목을 입력해 줘")
            self.english_title_entry.focus_set()
            return None
        return self._query(title_override=title, language_override=language_override)

    def _query(self, title_override: str | None = None, language_override: str | None = None) -> Query | None:
        title = (title_override if title_override is not None else self.title.get()).strip()
        if not title:
            messagebox.showinfo("작품명", "애니 제목을 먼저 입력해 줘")
            return None
        language = language_override or self.language.get()
        return Query(title=title, language=language)

    def _run(self, message: str, task, error_title: str = ""):
        if self.busy:
            return
        self.busy = True
        self.generation += 1
        generation = self.generation
        self.status.set(message)
        self.search_button.configure(state="disabled")
        self.title_search_button.configure(state="disabled")
        self.anime_search_button.configure(state="disabled")
        self.creator_blog_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.bigfile_button.configure(state="disabled")
        self.download_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        def worker():
            try:
                self.events.put((generation, "done", task()))
            except Exception as exc:
                self.events.put((generation, "error", (error_title, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def search(self):
        if self.busy:
            return
        query = self._query(language_override="ko")
        if not query:
            return
        self._run("애니시아 작품 검색 중…", lambda: ("anime", self.reanime.discover(query.title)))

    def search_title(self):
        if self.busy:
            return
        query = self._query(language_override="ko")
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
        query = self._query(language_override="ko")
        if query is None:
            return
        query = Query(title=anime.subject, language=query.language)
        self.title.set(anime.subject)
        self.current_query = query
        self._run(f"{anime.subject} 자막 검색 중…", lambda: ("search", self.engine.search_selected(query, anime)))

    def open_creator_blog(self):
        if self.busy:
            return
        selected = self.anime_tree.selection()
        anime = next((item for item in self.anime_results.anime
                      if selected and str(item.anime_no) == selected[0]), None)
        if anime is None:
            messagebox.showinfo("원본 블로그", "먼저 애니시아 작품을 선택해 줘")
            return
        self._run(f"{anime.subject} 제작자 블로그 확인 중…",
                  lambda: ("blogs", self.reanime.creator_blogs(anime)))

    def _show_creator_blogs(self, blogs: list[CreatorBlog]):
        if not blogs:
            self.status.set("선택한 작품의 애니시아 제작자 목록에 열 수 있는 블로그 주소가 없어")
            return
        if len(blogs) == 1:
            webbrowser.open(blogs[0].url)
            self.status.set(f"{blogs[0].creator} 블로그를 브라우저에서 열었어")
            return
        popup = tk.Toplevel(self.root)
        popup.title("원본 블로그 선택")
        popup.transient(self.root)
        popup.geometry("660x280")
        content = ttk.Frame(popup, padding=12)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="열 원본 블로그를 선택해 줘").pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(content, columns=("creator", "url"), show="headings", selectmode="browse")
        tree.heading("creator", text="제작자")
        tree.heading("url", text="블로그 주소")
        tree.column("creator", width=130, stretch=False)
        tree.column("url", width=480)
        tree.pack(fill="both", expand=True)
        for index, blog in enumerate(blogs):
            tree.insert("", "end", iid=str(index), values=(blog.creator, blog.url))
        tree.selection_set("0")

        def open_selected(_event=None):
            selected = tree.selection()
            if selected:
                blog = blogs[int(selected[0])]
                webbrowser.open(blog.url)
                self.status.set(f"{blog.creator} 블로그를 브라우저에서 열었어")
                popup.destroy()

        tree.bind("<Double-1>", open_selected)
        tree.bind("<Return>", open_selected)
        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="브라우저에서 열기", command=open_selected).pack(side="left")
        ttk.Button(buttons, text="닫기", command=popup.destroy).pack(side="right")
        popup.grab_set()
        popup.focus_set()

    def search_open(self):
        if self.busy:
            return
        query = self._english_query()
        if not query:
            return
        self.current_query = query
        self._run("OpenSubtitles 검색 중…", lambda: ("search", self.engine.search_open(query)))

    def search_bigfile(self):
        if self.busy:
            return
        query = self._english_query(language_override="ko")
        if query is None:
            return
        self.current_query = query
        self._run("Bigfile 애니 자막 검색 중…", lambda: ("search", self.engine.search_bigfile(query)))

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
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("자막 후보", "먼저 자막 파일을 선택해 줘")
            return
        if not self.current_query:
            messagebox.showerror("다운로드 실패", "검색 조건을 확인할 수 없습니다. 자막을 다시 검색해 주세요.")
            return
        candidate = self.results.candidates[int(selected[0])]
        if candidate.provider == "Bigfile" and not candidate.download_url:
            webbrowser.open(candidate.source_url)
            self.status.set("Bigfile 사이트에서 '애니'를 선택하고 영문 제목으로 다시 검색한 뒤 로그인하여 다운로드해 줘")
            return
        query = self.current_query
        self._run("선택한 자막 다운로드 중…", lambda: ("download", self.downloader.download(candidate, query)),
                  error_title="다운로드 실패")

    def _download_on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self.download()

    def _candidate_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        self._select()
        candidate = self.results.candidates[int(row)]
        label = "Bigfile에서 다운로드" if candidate.provider == "Bigfile" and not candidate.download_url else "자막 다운로드"
        self.candidate_menu.entryconfigure(0, label=label)
        try:
            self.candidate_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.candidate_menu.grab_release()

    def open_source(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("자막 후보", "먼저 자막 후보를 선택해 줘")
            return
        candidate = self.results.candidates[int(selected[0])]
        if candidate.source_url:
            webbrowser.open(candidate.source_url)

    def cancel(self):
        if not self.busy:
            return
        self.generation += 1
        self.busy = False
        self.search_button.configure(state="normal")
        self.title_search_button.configure(state="normal")
        self.anime_search_button.configure(state="normal" if self.anime_tree.selection() else "disabled")
        self.creator_blog_button.configure(state="normal" if self.anime_tree.selection() else "disabled")
        self.open_button.configure(state="normal")
        self.bigfile_button.configure(state="normal")
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
        self.creator_blog_button.configure(state="disabled")

    def _select_anime(self, _event=None):
        self.anime_search_button.configure(state="normal" if self.anime_tree.selection() and not self.busy else "disabled")
        self.creator_blog_button.configure(state="normal" if self.anime_tree.selection() and not self.busy else "disabled")

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
                self.creator_blog_button.configure(state="normal" if self.anime_tree.selection() else "disabled")
                self.open_button.configure(state="normal")
                self.bigfile_button.configure(state="normal")
                self.download_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if kind == "error":
                    title, description = value if isinstance(value, tuple) else ("", value)
                    self.status.set("작업 실패: " + description)
                    if title:
                        messagebox.showerror(title, description + "\n\n원문을 확인하려면 '선택 원문 열기'를 눌러 주세요.")
                    continue
                operation, outcome = value
                if operation == "download":
                    path, note = outcome
                    self.status.set("저장됨: " + str(path) + (" · " + note if note else ""))
                    messagebox.showinfo("다운로드 완료", str(path) + ("\n\n" + note if note else ""))
                elif operation == "anime":
                    self.anime_results = outcome
                    self._populate_anime()
                    self.results = SearchResult()
                    self.current_query = None
                    self._populate()
                    warning = " · " + outcome.warnings[0] if outcome.warnings else ""
                    self.status.set(f"작품 {len(outcome.anime)}개 · 작품을 선택해 자막 검색{warning}" if outcome.anime else
                                    f"검색된 작품 없음 · 정확한 제목이면 입력 제목으로 애니시아 자막 검색{warning}")
                elif operation == "blogs":
                    self._show_creator_blogs(outcome)
                else:
                    self.results = outcome
                    self._populate()
                    important = next((item for item in outcome.warnings if item.startswith(
                        ("OpenSubtitles", "Bigfile", "애니시아 작품 검색", "애니시아 작품 목록"))), None)
                    warning = " · " + (important or outcome.warnings[0]) if outcome.warnings else ""
                    self.status.set(f"후보 {len(outcome.candidates)}개{warning}")
        except Empty:
            pass
        self.root.after(120, self._poll)

    def configure_key(self):
        value = simpledialog.askstring("OpenSubtitles API 키", "본인 OpenSubtitles API 키를 입력해 줘 (빈칸은 기존 키 삭제)",
                                       parent=self.root, show="*")
        if value is None:
            return
        self.config["opensubtitles_api_key"] = value.strip()
        self.open.api_key = value.strip()
        self.save_settings()
        self.status.set("OpenSubtitles API 키 설정 저장됨" if value.strip() else "OpenSubtitles API 키 삭제됨")

    def show_key_help(self):
        popup = tk.Toplevel(self.root)
        popup.title("OpenSubtitles API 키 발급 방법")
        popup.transient(self.root)
        popup.resizable(False, False)
        content = ttk.Frame(popup, padding=20)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="OpenSubtitles API 키 발급 방법", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            content,
            text=(
                "1. OpenSubtitles.com에 가입하거나 로그인합니다.\n"
                "2. 프로필의 'API Consumers' 메뉴를 엽니다.\n"
                "3. 새 API Consumer를 만들고 발급된 API 키를 복사합니다.\n"
                "4. 이 앱의 'OpenSubtitles API 키' 버튼에서 복사한 키를 등록합니다."
            ),
            justify="left",
            wraplength=470,
        ).pack(anchor="w", pady=(14, 10))
        ttk.Label(content, text="키는 다른 사람에게 공개하지 마세요.", wraplength=470).pack(anchor="w")
        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(
            buttons, text="API Consumers 페이지 열기",
            command=lambda: webbrowser.open("https://www.opensubtitles.com/en/consumers"),
        ).pack(side="left")
        ttk.Button(buttons, text="닫기", command=popup.destroy).pack(side="right")
        popup.grab_set()
        popup.focus_set()

    def open_folder(self):
        folder = self.folder / "subtitles"
        folder.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder))
        else:
            self.status.set(str(folder))

    def google_open(self):
        query = self._query()
        if query:
            webbrowser.open(self._google_search_url(query))

    @staticmethod
    def _google_search_url(query: Query) -> str:
        from urllib.parse import urlencode
        words = f"{query.title} {str(query.episode) + '화 ' if query.episode else ''}자막"
        return "https://www.google.com/search?" + urlencode({"hl": "ko", "q": words})

    def close(self):
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
