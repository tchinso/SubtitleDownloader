"""Optional Edge companion bridge. Title search never depends on this server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import re
import threading


PORT = 48741
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


class BridgeServer:
    def __init__(self, token: str, inbox: Queue, port: int = PORT):
        self.token, self.inbox, self.port = token, inbox, port
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                origin = self.headers.get("Origin", "")
                if EXTENSION_ORIGIN.fullmatch(origin):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

            def _reply(self, status: int, payload: dict):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                origin = self.headers.get("Origin")
                if origin and not EXTENSION_ORIGIN.fullmatch(origin):
                    self._reply(403, {"error": "허용되지 않은 브라우저 출처"})
                    return False
                if self.headers.get("Host") != f"127.0.0.1:{parent.port}":
                    self._reply(403, {"error": "허용되지 않은 호스트"})
                    return False
                if self.headers.get("Authorization", "") != "Bearer " + parent.token:
                    self._reply(401, {"error": "연결 코드가 다름"})
                    return False
                return True

            def do_OPTIONS(self):
                if not EXTENSION_ORIGIN.fullmatch(self.headers.get("Origin", "")):
                    self._reply(403, {"error": "허용되지 않은 출처"})
                    return
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == "/v1/status":
                    self._reply(200, {"ok": True, "version": 1})
                else:
                    self._reply(404, {"error": "없는 경로"})

            def do_POST(self):
                if not self._authorized():
                    return
                if self.path != "/v1/send":
                    self._reply(404, {"error": "없는 경로"})
                    return
                size = self.headers.get("Content-Length", "")
                if not size.isdecimal() or not 0 < int(size) <= 4096:
                    self._reply(400, {"error": "요청 크기가 올바르지 않음"})
                    return
                try:
                    body = json.loads(self.rfile.read(int(size)))
                    title, url = body.get("title", ""), body.get("url", "")
                    if not isinstance(title, str) or not isinstance(url, str) or len(title) > 500 or len(url) > 2000:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    self._reply(400, {"error": "작품 정보를 읽을 수 없음"})
                    return
                parent.inbox.put({"title": title.strip(), "url": url.strip()})
                self._reply(200, {"ok": True})

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="EdgeBridge", daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
