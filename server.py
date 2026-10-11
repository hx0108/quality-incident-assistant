"""Local generic AI chat server. Uses only the Python standard library."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
SYSTEM_MESSAGE = (
    "你是通用信息整理助手。根据用户提供的信息清晰回答，区分已知信息和待核实事项。"
    "不要假设任何组织的内部政策、判断阈值或授权。没有依据时明确说明不确定。"
    "输出仅为辅助草稿，不作正式业务认定，也不执行外部操作。"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, busy INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, state TEXT NOT NULL,
                    error_code TEXT, created_at TEXT NOT NULL
                );
            """)
            # Never retry a possibly charged request after process interruption.
            db.execute("UPDATE messages SET state='ERROR', error_code='MODEL_OUTCOME_UNKNOWN' WHERE state='PENDING'")
            db.execute("UPDATE conversations SET busy=0 WHERE busy=1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def access(db, owner, conversation_id):
        row = db.execute("SELECT * FROM conversations WHERE id=? AND owner=?", (conversation_id, owner)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "对话不存在或不可访问。")
        return row

    def listing(self, owner):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,title,created_at FROM conversations WHERE owner=? ORDER BY created_at DESC LIMIT 100", (owner,))]

    def create(self, owner):
        row = {"id": str(uuid.uuid4()), "title": "新对话", "created_at": now()}
        with self.connect() as db:
            db.execute("INSERT INTO conversations(id,owner,title,created_at) VALUES(?,?,?,?)",
                       (row["id"], owner, row["title"], row["created_at"]))
        return row

    def detail(self, owner, conversation_id):
        with self.connect() as db:
            row = self.access(db, owner, conversation_id)
            return {"id": row["id"], "title": row["title"], "busy": bool(row["busy"]),
                    "messages": [dict(m) for m in db.execute(
                        "SELECT id,role,content,state,error_code,created_at FROM messages WHERE conversation_id=? ORDER BY rowid",
                        (conversation_id,))]}

    def start_turn(self, owner, conversation_id, content):
        message_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.access(db, owner, conversation_id)
            if row["busy"]:
                raise ApiError(409, "CONVERSATION_BUSY", "当前对话正在处理，请等待完成。")
            db.execute("UPDATE conversations SET busy=1,title=? WHERE id=?",
                       (content[:36] if row["title"] == "新对话" else row["title"], conversation_id))
            for role, text, state, identifier in [("user", content, "READY", str(uuid.uuid4())),
                                                  ("assistant", "", "PENDING", message_id)]:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                           (identifier, conversation_id, role, text, state, None, now()))
        return message_id

    def finish_turn(self, conversation_id, message_id, content="", error=None):
        with self.connect() as db:
            db.execute("UPDATE messages SET content=?,state=?,error_code=? WHERE id=? AND state='PENDING'",
                       (content, "ERROR" if error else "READY", error, message_id))
            db.execute("UPDATE conversations SET busy=0 WHERE id=?", (conversation_id,))


def now():
    return datetime.now(timezone.utc).isoformat()


def model_reply(messages):
    url, key, model = (os.environ.get(name, "").strip() for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_NAME"))
    if not all((url, key, model)):
        raise ApiError(503, "MODEL_NOT_CONFIGURED", "模型接口尚未配置，消息已保存。")
    parsed = urllib.parse.urlparse(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ApiError(503, "MODEL_CONFIG_INVALID", "模型地址配置不符合要求。")
    history = [{"role": m["role"], "content": m["content"]} for m in messages if m["state"] == "READY"]
    if sum(len(m["content"]) for m in history) > 32000:
        raise ApiError(413, "CONTEXT_LIMIT", "对话上下文过长，请新建对话；没有发起模型调用。")
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM_MESSAGE}, *history],
               "max_tokens": 1200, "stream": False}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    # Redirects could send authorization to an unintended host; refuse them.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversized response")
        content = json.loads(raw)["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 24000:
            raise ValueError("invalid response")
        return content
    except urllib.error.HTTPError as error:
        # Do not expose provider bodies, account identifiers, or credentials.
        error.close()
        raise ApiError(502, "MODEL_REQUEST_FAILED", "模型接口拒绝了请求；消息已保存，没有自动重试。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError(502, "MODEL_OUTCOME_UNKNOWN", "模型响应未确认；请核对服务端记录，没有自动重试。") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ApiError(502, "MODEL_RESPONSE_INVALID", "模型返回格式无效；消息已保存，没有自动重试。") from None


def build_server(port=4312, runtime=RUNTIME):
    runtime.mkdir(parents=True, exist_ok=True)
    secret_path = runtime / "session.key"
    try:
        with secret_path.open("xb") as f:
            f.write(secrets.token_bytes(32))
        if os.name != "nt":
            secret_path.chmod(0o600)
    except FileExistsError:
        pass
    secret = secret_path.read_bytes()
    store = Store(runtime / "conversations.sqlite3")
    slots = threading.BoundedSemaphore(2)

    def signature(value):
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def check_origin(self, write=False):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            if host not in hosts or self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise ApiError(403, "ORIGIN_REJECTED", "请求来源不受信任。")
            origin = self.headers.get("Origin")
            if (write and not origin) or (origin and origin != "http://" + host):
                raise ApiError(403, "ORIGIN_REJECTED", "请求来源不受信任。")

        def owner(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie["assistant_session"].value
                owner, mac = token.split(".", 1)
                if len(owner) == 32 and hmac.compare_digest(mac, signature(owner)):
                    return owner
            except (KeyError, ValueError):
                pass
            return None

        def respond(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
            raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 64000:
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (ValueError, UnicodeError):
                raise ApiError(400, "INVALID_INPUT", "输入格式无效或内容过长。") from None

        def dispatch(self, write=False):
            self.check_origin(write)
            path = urllib.parse.urlparse(self.path).path
            if not write and path == "/api/session":
                owner = self.owner() or secrets.token_hex(16)
                self.respond(200, {"csrf_token": signature(owner + ":csrf")},
                             cookie=f"assistant_session={owner}.{signature(owner)}; Path=/; HttpOnly; SameSite=Strict")
                return
            if path.startswith("/api/"):
                owner = self.owner()
                if owner is None:
                    raise ApiError(401, "SESSION_REQUIRED", "请刷新页面重新建立会话。")
                if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), signature(owner + ":csrf")):
                    raise ApiError(403, "CSRF_REJECTED", "请求校验失败。")
                if path == "/api/conversations":
                    self.respond(201 if write else 200, store.create(owner) if write else {"items": store.listing(owner)})
                    return
                parts = path.strip("/").split("/")
                if len(parts) in {3, 4} and parts[:2] == ["api", "conversations"]:
                    conversation_id = parts[2]
                    if len(parts) == 3 and not write:
                        self.respond(200, store.detail(owner, conversation_id))
                        return
                    if len(parts) == 4 and parts[3] == "messages" and write:
                        data = self.body()
                        content = data.get("content")
                        if set(data) != {"content"} or not isinstance(content, str) or not content.strip() or len(content) > 8000:
                            raise ApiError(400, "INVALID_INPUT", "请输入不超过8000字的有效内容。")
                        if not slots.acquire(blocking=False):
                            raise ApiError(429, "BUSY", "当前服务繁忙；没有发起模型调用。")
                        message_id = None
                        try:
                            message_id = store.start_turn(owner, conversation_id, content.strip())
                            answer = model_reply(store.detail(owner, conversation_id)["messages"])
                            store.finish_turn(conversation_id, message_id, answer)
                        except ApiError as error:
                            if message_id:
                                store.finish_turn(conversation_id, message_id, error=error.code)
                            raise
                        except Exception:
                            if message_id:
                                store.finish_turn(conversation_id, message_id, error="SERVER_ERROR")
                            raise
                        finally:
                            slots.release()
                        self.respond(200, store.detail(owner, conversation_id))
                        return
                raise ApiError(404, "NOT_FOUND", "接口不存在。")
            static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/styles.css": ("styles.css", "text/css")}
            if not write and path in static:
                name, mime = static[path]
                self.respond(200, (ROOT / "web" / name).read_bytes(), mime + "; charset=utf-8")
                return
            raise ApiError(404, "NOT_FOUND", "页面不存在。")

        def handle_request(self, write=False):
            try:
                self.dispatch(write)
            except ApiError as error:
                self.respond(error.status, {"code": error.code, "message": error.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                # Details stay private. Never reflect arbitrary exception text.
                self.respond(500, {"code": "SERVER_ERROR", "message": "服务处理失败，请检查本机服务状态。"})

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(True)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the generic local AI assistant")
    parser.add_argument("--port", type=int, default=4312)
    args = parser.parse_args()
    server = build_server(args.port)
    print(f"Open http://127.0.0.1:{server.server_port} — local development only")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
