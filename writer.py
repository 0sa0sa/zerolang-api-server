#!/usr/bin/env python3
"""External writer sidecar for the Zerolang Todo BE.

Why this exists: Zero 0.3.4's host (darwin-arm64) direct backend can READ files
inside an HTTP handler (`std.fs.readBytes`) but cannot WRITE them (every fs-write
IR value fails BLD004), and the HTTP runtime only exists on the host target. So the
Zero server owns READS (GET /todos re-reads data/store.json live) and this sidecar
owns WRITES + persistence. It is the single public entrypoint (default :8080):

  GET  /health    -> proxied to the Zero backend
  GET  /todos     -> proxied to the Zero backend (which reads data/store.json)
  POST /todos     -> append {title} with an auto-increment id, persist, 201
  PATCH /todos/ID -> toggle `done`, persist, 200
  DELETE /todos/ID-> remove, persist, 204
"""
import json, os, re, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STORE   = os.environ.get("ZT_STORE", os.path.join(os.path.dirname(__file__), "data", "store.json"))
BACKEND = os.environ.get("ZERO_BACKEND", "127.0.0.1:3000")   # host:port of `zero run`
PORT    = int(os.environ.get("ZT_PORT", "8080"))

_lock = threading.Lock()


def load():
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(todos):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STORE)   # atomic


def next_id(todos):
    return (max((t.get("id", 0) for t in todos), default=0)) + 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        try:
            with urllib.request.urlopen(f"http://{BACKEND}{self.path}", timeout=5) as r:
                body = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            self._json(502, {"error": "backend_unreachable", "detail": str(e)})

    def _id_from_path(self):
        m = re.fullmatch(r"/todos/(\d+)", self.path)
        return int(m.group(1)) if m else None

    def do_GET(self):
        if self.path in ("/health", "/todos"):
            self._proxy()            # Zero serves reads
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/todos":
            return self._json(404, {"error": "not_found"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "expected_json_body"})
        title = payload.get("title")
        if not isinstance(title, str) or not title:
            return self._json(400, {"error": "missing_title"})
        with _lock:
            todos = load()
            todo = {"id": next_id(todos), "title": title, "done": False}
            todos.append(todo)
            save(todos)
        self._json(201, todo)

    def do_PATCH(self):
        tid = self._id_from_path()
        if tid is None:
            return self._json(404, {"error": "not_found"})
        with _lock:
            todos = load()
            for t in todos:
                if t.get("id") == tid:
                    t["done"] = not t.get("done", False)
                    save(todos)
                    return self._json(200, t)
        self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        tid = self._id_from_path()
        if tid is None:
            return self._json(404, {"error": "not_found"})
        with _lock:
            todos = load()
            kept = [t for t in todos if t.get("id") != tid]
            if len(kept) == len(todos):
                return self._json(404, {"error": "not_found"})
            save(kept)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not os.path.exists(STORE):
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        save([])
    print(f"writer: http://127.0.0.1:{PORT}  store={STORE}  backend={BACKEND}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
