#!/usr/bin/env python3
"""Zerolang Todo BE の外部ライター(サイドカー)。

なぜ存在するか: Zero 0.3.4 のホスト(darwin-arm64)直接バックエンドは、HTTP
ハンドラ内でファイルを READ できる(`std.fs.readBytes`)が WRITE できない
(fs書き込みIRはすべて BLD004 で失敗)。さらに HTTP ランタイムはホスト
ターゲットにしか存在しない。そこで Zero サーバーは READ(GET /todos が
data/store.json を毎回読む)を担当し、このサイドカーが WRITE と永続化を担う。
公開エントリポイントはこのプロセス1つ(既定 :8080):

  GET  /health    -> Zero バックエンドへプロキシ
  GET  /todos     -> Zero バックエンドへプロキシ(Zero が data/store.json を読む)
  POST /todos     -> {title} を自動採番idで追加・保存し 201
  PATCH /todos/ID -> `done` を反転・保存し 200
  DELETE /todos/ID-> 削除・保存し 204
"""
import json, os, re, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- 設定(すべて環境変数で上書き可。run.sh が設定する) --------------------
# STORE: このプロセスが所有し、Zero バックエンドが読む JSON 配列ファイル。
STORE   = os.environ.get("ZT_STORE", os.path.join(os.path.dirname(__file__), "data", "store.json"))
# BACKEND: `zero run` が待ち受けている host:port(ポートは自動で繰り上がる)。
BACKEND = os.environ.get("ZERO_BACKEND", "127.0.0.1:3000")
# PORT: このサイドカーの公開ポート — クライアントが話す唯一のポート。
PORT    = int(os.environ.get("ZT_PORT", "8080"))

# ストアの read-modify-write を直列化し、同時リクエストが交錯して壊さない
# ようにする(ThreadingHTTPServer は各リクエストを別スレッドで処理する)。
_lock = threading.Lock()


def load():
    """ストアを読み todo のリストを返す(無い/壊れていれば [])。"""
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(todos):
    """todos をアトミックに保存: 一時ファイルに書いてから本体へ rename する。
    os.replace はアトミックなので、読み手(Zero バックエンド)が書きかけの
    ファイルを見ることはない。"""
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STORE)


def next_id(todos):
    """次の自動採番id = 既存の最大id + 1(空なら 1)。"""
    return (max((t.get("id", 0) for t in todos), default=0)) + 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive。正しい Content-Length が必要

    def _json(self, code, obj):
        """ステータスと Content-Length を明示して JSON 応答を送る。"""
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        """現在のリクエストを Zero バックエンドへ転送し、その応答を中継する。
        これが「読みは本当に Zero 経由」を保つ要: Zero が落ちていれば
        クライアントは 502 を受け取り、読みがここから返っていないと分かる。"""
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
        """/todos/<id> から数値idを取り出す。一致しなければ None。"""
        m = re.fullmatch(r"/todos/(\d+)", self.path)
        return int(m.group(1)) if m else None

    def do_GET(self):
        # 読みは Zero バックエンドへ委譲。それ以外は 404。
        if self.path in ("/health", "/todos"):
            self._proxy()
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        # todo を作成。ここで有効なパスは /todos のみ。
        if self.path != "/todos":
            return self._json(404, {"error": "not_found"})
        # Content-Length バイト分だけ読み、JSON として解析。
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "expected_json_body"})
        # 空でない文字列の title を必須とする。
        title = payload.get("title")
        if not isinstance(title, str) or not title:
            return self._json(400, {"error": "missing_title"})
        # ロック下で read-modify-write し、idとファイルの整合を保つ。
        with _lock:
            todos = load()
            todo = {"id": next_id(todos), "title": title, "done": False}
            todos.append(todo)
            save(todos)
        self._json(201, todo)   # 201 Created、保存した todo をエコー

    def do_PATCH(self):
        # /todos/<id> の `done` フラグを反転。
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
        # 該当idの todo が無い。
        self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        # /todos/<id> を削除。
        tid = self._id_from_path()
        if tid is None:
            return self._json(404, {"error": "not_found"})
        with _lock:
            todos = load()
            # 対象id以外を残す。
            kept = [t for t in todos if t.get("id") != tid]
            if len(kept) == len(todos):   # 何も消えなかった -> 不明なid
                return self._json(404, {"error": "not_found"})
            save(kept)
        # 204 No Content(ボディ無し)。
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass   # 既定のリクエストごとアクセスログ(stderr)を抑制


if __name__ == "__main__":
    # 最初の GET が読めるよう、ストアが無ければ用意する。
    if not os.path.exists(STORE):
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        save([])
    print(f"writer: http://127.0.0.1:{PORT}  store={STORE}  backend={BACKEND}")
    # ThreadingHTTPServer: リクエストごとに1スレッド(ゆえに上の _lock)。
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
