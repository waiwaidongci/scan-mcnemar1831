"""HTTP 服务：标准库 http.server，JSON API + 静态前端 + CSV 导出。"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db
from .audit import record
from .config import ARTIFACT_DIR, HOST, PORT, ROLE_LABEL, TICKET_TYPES, ensure_dirs
from .domain import DomainError
from .security import issue_token, matches, verify_token
from .services import acceptance, datasets as dataset_service, reports
from .services.thresholds import active_set, list_sets, preview, seal, set_view
from pathlib import Path

from .worker import tick

log = logging.getLogger("credit_release.http")
ensure_dirs()
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend"

ROLE_WRITE = {"admin", "analyst"}
ROLE_SEAL = {"admin"}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "CreditRelease/1.0"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # ---------- 基础工具 ----------
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, content, content_type, status=200, extra_headers=None, filename=None):
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if filename:
            from urllib.parse import quote
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(filename)}")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        if length > 5 * 1024 * 1024:
            raise HttpError(413, "上传内容超过 5MB 限制")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HttpError(400, "请求体需为 UTF-8 JSON")
        if not isinstance(payload, dict):
            raise HttpError(400, "请求体需为 JSON 对象")
        return payload

    def _actor(self, *allowed_roles):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HttpError(401, "请先登录或重新登录")
        principal = verify_token(header[len("Bearer "):].strip())
        if not principal:
            raise HttpError(401, "登录已过期，请重新登录")
        if allowed_roles and principal["role"] not in allowed_roles:
            raise HttpError(403, "当前角色没有此操作权限")
        return principal

    # ---------- 路由 ----------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/healthz":
                return self._json({"status": "ok"})
            if path.startswith("/api/"):
                return self._api(method, path, query)
            if method == "GET":
                return self._static(path)
            raise HttpError(404, "接口不存在")
        except HttpError as error:
            self._json({"detail": error.message}, error.status)
        except DomainError as error:
            self._json({"detail": str(error)}, 400)
        except Exception:
            log.exception("未处理异常 %s %s", method, path)
            self._json({"detail": "服务内部错误，请查看后端日志"}, 500)

    # ---------- 静态资源 ----------
    def _static(self, path):
        from pathlib import Path
        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        root = STATIC_DIR.resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            target = root / "index.html"
        suffix = target.suffix
        mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                ".json": "application/json"}.get(suffix, "application/octet-stream")
        self._text(target.read_bytes(), mime, extra_headers={"Cache-Control": "no-cache"})

    # ---------- API ----------
    def _api(self, method, path, query):
        route = path[len("/api/"):]

        if method == "POST" and route == "auth/login":
            return self._login()
        if method == "GET" and route == "auth/me":
            return self._json(self._actor())
        if method == "GET" and route == "spec":
            return self._spec()
        if method == "GET" and route == "template":
            self._actor()
            return self._text(reports.template_csv(), "text/csv; charset=utf-8",
                               filename="置信分放行导入模板.csv")

        if method == "GET" and route == "datasets":
            self._actor()
            return self._list_datasets()
        if method == "POST" and route == "datasets":
            actor = self._actor(*ROLE_WRITE)
            return self._import_dataset(actor)
        if method == "GET" and route.startswith("datasets/"):
            self._actor()
            return self._dataset_detail(route.split("/", 1)[1], query)

        if method == "POST" and route == "thresholds/preview":
            self._actor(*ROLE_WRITE)
            return self._threshold_preview()
        if method == "POST" and route == "thresholds/seal":
            actor = self._actor(*ROLE_SEAL)
            return self._threshold_seal(actor)
        if method == "GET" and route == "thresholds":
            self._actor()
            return self._list_thresholds()

        if method == "GET" and route == "acceptance/batches":
            self._actor()
            return self._list_acceptance_batches()
        if method == "GET" and route == "acceptance/attempts":
            self._actor()
            return self._list_attempts()
        if method == "POST" and route == "acceptance/run":
            actor = self._actor(*ROLE_WRITE)
            return self._acceptance_run(actor)
        if method == "POST" and route.startswith("acceptance/") and route.endswith("/resume"):
            actor = self._actor(*ROLE_WRITE)
            return self._acceptance_resume(route.split("/")[1], actor)
        if method == "GET" and route.startswith("acceptance/"):
            self._actor()
            return self._acceptance_detail(route.split("/")[1])

        if method == "GET" and route.startswith("exports/"):
            self._actor()
            return self._export(route.split("/", 1)[1])
        if method == "GET" and route == "audit":
            actor = self._actor("admin")
            return self._audit(query)
        raise HttpError(404, "接口不存在")

    # ---------- 鉴权 ----------
    def _login(self):
        body = self._body()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            raise HttpError(400, "请输入账号和密码")
        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not matches(password, row["password_hash"]):
            raise HttpError(401, "账号或密码不正确")
        with db.tx() as conn:
            record(conn, username, "auth.login", username, role=row["role"])
        return self._json({"username": username, "role": row["role"],
                           "role_label": {"admin": "复核主管", "analyst": "分析员",
                                          "viewer": "只读查看"}[row["role"]],
                           "token": issue_token(username, row["role"])})

    def _spec(self):
        return self._json({
            "title": "票据置信分放行阈值调参与验收",
            "ticket_types": TICKET_TYPES,
            "dataset_roles": [{"key": key, "label": ROLE_LABEL[key]} for key in ROLE_LABEL],
            "csv_fields": [{"key": key, "label": label} for key, label in
                           [("batch_id", "批号"), ("ticket_id", "票号"), ("ticket_type", "票种"),
                            ("score_b", "B 置信分"), ("a_release", "A 是否放行"),
                            ("manual_qualified", "人工真值是否合格")]],
            "score_range": [0, 100],
            "rule_summary": [
                "调参批与验收批不得共用批号或票号。",
                "每票种在错放上限内选合格放行数最大的阈值，相同放行数取更严阈值。",
                "阈值仅在调参批封存；首版封存前不得导入验收批。",
                "验收通过条件：各票种错放均不超上限，且 B 合格放行总数严格多于 A。",
                "并发验收同批只认一套已封存阈值；调参更正后须换未揭晓的新验收批再验。",
                "中断续跑沿用原阈值和数据版本；被取代版本的旧尝试不得发布。",
            ],
        })

    # ---------- 数据批次 ----------
    def _list_datasets(self):
        with db.read_conn() as conn:
            rows = dataset_service.list_datasets(conn)
            return self._json([{
                "id": r["id"], "role": r["role"], "role_label": ROLE_LABEL[r["role"]],
                "batch_id": r["batch_id"], "name": r["name"], "filename": r["filename"],
                "checksum": r["checksum"], "row_count": r["row_count"],
                "revealed": bool(r["revealed"]), "author": r["author"],
                "created_at": r["created_at"],
            } for r in rows])

    def _import_dataset(self, actor):
        body = self._body()
        role = body.get("role")
        if role not in ROLE_LABEL:
            raise HttpError(400, "批次角色须为 tuning 或 acceptance")
        name = str(body.get("name", "")).strip()
        filename = str(body.get("filename", "data.csv")).strip() or "data.csv"
        csv_text = body.get("csv_text")
        if not name:
            raise HttpError(400, "请填写批次名称")
        if not isinstance(csv_text, str):
            raise HttpError(400, "请提供 csv_text 文本内容")
        with db.tx() as conn:
            dataset_id, batch_id, signature, count = dataset_service.import_dataset(
                conn, role, name, filename, csv_text, actor["username"])
            record(conn, actor["username"], "dataset.imported", dataset_id,
                   role=role, batch_id=batch_id, rows=count, checksum=signature)
        return self._json({"id": dataset_id, "batch_id": batch_id, "checksum": signature,
                           "row_count": count, "created": True}, 201)

    def _dataset_detail(self, dataset_id, query):
        if not dataset_id.isdigit():
            raise HttpError(404, "接口不存在")
        offset = max(0, int(query.get("offset", ["0"])[0]))
        limit = min(100, max(1, int(query.get("limit", ["20"])[0])))
        with db.read_conn() as conn:
            row = dataset_service.get_dataset(conn, int(dataset_id))
            all_rows = db.loads(row["rows_json"])
        return self._json({
            "id": row["id"], "role": row["role"], "role_label": ROLE_LABEL[row["role"]],
            "batch_id": row["batch_id"], "name": row["name"], "checksum": row["checksum"],
            "row_count": row["row_count"], "revealed": bool(row["revealed"]),
            "rows": all_rows[offset:offset + limit], "offset": offset, "limit": limit,
        })

    # ---------- 阈值 ----------
    def _caps(self, body):
        caps = body.get("caps")
        if caps is None:
            caps = {ticket_type: 2 for ticket_type in TICKET_TYPES}
        if not isinstance(caps, dict):
            raise HttpError(400, "caps 需为票种到非负整数的映射")
        normalized = {}
        for ticket_type in TICKET_TYPES:
            value = caps.get(ticket_type, 2)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HttpError(400, f"{ticket_type} 的错放上限需为非负整数")
            normalized[ticket_type] = value
        return normalized

    def _threshold_preview(self):
        body = self._body()
        dataset_id = body.get("dataset_id")
        if not isinstance(dataset_id, int):
            raise HttpError(400, "dataset_id 需为整数")
        with db.read_conn() as conn:
            return self._json(preview(conn, dataset_id, self._caps(body)))

    def _threshold_seal(self, actor):
        body = self._body()
        dataset_id = body.get("dataset_id")
        if not isinstance(dataset_id, int):
            raise HttpError(400, "dataset_id 需为整数")
        caps = self._caps(body)
        with db.tx() as conn:
            new_id, version, sealed = seal(conn, dataset_id, caps, actor["username"])
        return self._json({"id": new_id, "version": version, "sealed": sealed}, 201)

    def _list_thresholds(self):
        with db.read_conn() as conn:
            return self._json([set_view(r) for r in list_sets(conn)])

    # ---------- 验收 ----------
    def _list_acceptance_batches(self):
        with db.read_conn() as conn:
            rows = acceptance.list_batches(conn)
            return self._json([{
                "id": r["id"], "dataset_id": r["dataset_id"], "batch_id": r["batch_id"],
                "row_count": r["row_count"], "checksum": r["checksum"],
                "threshold_set_id": r["threshold_set_id"], "threshold_version": r["threshold_version"],
                "threshold_status": r["threshold_status"], "bound_at": r["bound_at"],
                "bound_by": r["bound_by"],
            } for r in rows])

    def _list_attempts(self):
        with db.read_conn() as conn:
            rows = acceptance.list_attempts(conn)
            return self._json([self._attempt_view(r) for r in rows])

    def _acceptance_run(self, actor):
        body = self._body()
        dataset_id = body.get("dataset_id")
        if not isinstance(dataset_id, int):
            raise HttpError(400, "dataset_id 需为整数")
        with db.tx() as conn:
            attempt_id = acceptance.enqueue(conn, dataset_id, actor["username"])
        self._kick_worker()
        return self._json({"attempt_id": attempt_id, "status": "queued"}, 202)

    def _acceptance_resume(self, attempt_id, actor):
        if not attempt_id.isdigit():
            raise HttpError(404, "接口不存在")
        with db.tx() as conn:
            new_id = acceptance.resume_interrupted(conn, int(attempt_id), actor["username"])
        self._kick_worker()
        return self._json({"attempt_id": new_id, "resumed_from": int(attempt_id)}, 202)

    def _acceptance_detail(self, attempt_id):
        if not attempt_id.isdigit():
            raise HttpError(404, "接口不存在")
        with db.read_conn() as conn:
            row = acceptance.get_attempt(conn, int(attempt_id))
            return self._json(self._attempt_view(row))

    def _attempt_view(self, row):
        result = db.loads(row["result_json"])
        return {
            "id": row["id"], "status": row["status"], "attempt_no": row["attempt_no"],
            "resume_count": row["resume_count"], "batch_id": row["dataset_batch_id"],
            "threshold_version": row["threshold_version"], "dataset_id": row["dataset_id"],
            "dataset_checksum": row["dataset_checksum"], "threshold_set_id": row["threshold_set_id"],
            "publishable": bool(row["publishable"]), "export_file": row["export_file"],
            "error": row["error"], "author": row["author"],
            "created_at": row["created_at"], "finished_at": row["finished_at"],
            "result": result,
        }

    def _export(self, tail):
        attempt_id, _, suffix = tail.partition("/")
        if suffix != "export" or not attempt_id.isdigit():
            raise HttpError(404, "接口不存在")
        with db.read_conn() as conn:
            row = acceptance.get_attempt(conn, int(attempt_id))
            if row["status"] != "complete":
                raise HttpError(409, "只有已完成的验收尝试可以导出")
            if not row["publishable"]:
                raise HttpError(409, "该尝试基于已被取代的封存阈值，旧尝试不得发布")
            path = ARTIFACT_DIR / row["export_file"]
            if not path.is_file():
                content = reports.render_csv(row).encode("utf-8")
            else:
                content = path.read_bytes()
        self._text(content, "text/csv; charset=utf-8",
                   filename=f"置信分放行验收-尝试{attempt_id}.csv")

    def _audit(self, query):
        limit = min(200, max(1, int(query.get("limit", ["100"])[0])))
        with db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return self._json([{
                "id": r["id"], "actor": r["actor"], "event": r["event"], "target": r["target"],
                "info": db.loads(r["info_json"]), "created_at": r["created_at"],
            } for r in rows])

    def _kick_worker(self):
        # 进程内 worker 由后台线程轮询；这里同步再跑一拍以缩短演示等待。
        threading.Thread(target=tick, daemon=True).start()


def worker_loop():
    import time
    while True:
        try:
            tick()
        except Exception:
            log.exception("后台 worker 异常")
        time.sleep(1.0)


def create_server():
    ensure_dirs()
    db.init_db()
    from .config import SEED_DEMO
    if SEED_DEMO:
        with db.tx() as conn:
            from .seed import seed
            seeded = seed(conn)
        log.info("演示数据：%s", seeded)
    with db.tx() as conn:
        acceptance.recover_stale(conn)
    threading.Thread(target=worker_loop, daemon=True).start()
    return ThreadingHTTPServer((HOST, PORT), Handler)
