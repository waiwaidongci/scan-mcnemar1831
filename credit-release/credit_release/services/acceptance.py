"""验收服务：并发同批只认一套已封存阈值；中断续跑沿用原阈值与数据版本。"""

import threading
from datetime import datetime, timedelta, timezone

from .. import domain
from ..audit import record
from ..config import LEASE_SECONDS
from ..db import dumps, loads
from ..domain import DomainError, evaluate_acceptance
from ..timeutil import now
from .thresholds import active_set

_worker_lock = threading.Lock()


def _lease_expired(lease_until):
    if not lease_until:
        return True
    return datetime.fromisoformat(lease_until) < datetime.now(timezone.utc)


def bind_batch(conn, dataset_id, actor):
    """揭晓验收批：与当前生效封存阈值绑定。已揭晓批次不得换阈值复验。"""
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not dataset:
        raise DomainError("找不到该数据批次")
    if dataset["role"] != "acceptance":
        raise DomainError("只有验收批可以发起验收")
    sealed_row = active_set(conn)
    if not sealed_row:
        raise DomainError("尚无生效封存阈值，无法验收")

    existing = conn.execute(
        "SELECT * FROM acceptance_batches WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if existing:
        if existing["threshold_set_id"] != sealed_row["id"]:
            raise DomainError(
                "该验收批已揭晓并绑定另一套封存阈值；调参更正后请换尚未揭晓的新验收批再验")
        return existing, False

    cursor = conn.execute(
        """INSERT INTO acceptance_batches (dataset_id, threshold_set_id, bound_at, bound_by)
           VALUES (?, ?, ?, ?)""",
        (dataset_id, sealed_row["id"], now(), actor),
    )
    conn.execute("UPDATE datasets SET revealed = 1 WHERE id = ?", (dataset_id,))
    record(conn, actor, "acceptance.bound", cursor.lastrowid,
           dataset_id=dataset_id, batch_id=dataset["batch_id"],
           threshold_set_id=sealed_row["id"], version=sealed_row["version"])
    return conn.execute("SELECT * FROM acceptance_batches WHERE id = ?",
                        (cursor.lastrowid,)).fetchone(), True


def enqueue(conn, dataset_id, actor):
    batch, _ = bind_batch(conn, dataset_id, actor)
    sealed_row = conn.execute("SELECT * FROM threshold_sets WHERE id = ?",
                              (batch["threshold_set_id"],)).fetchone()
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?",
                           (batch["dataset_id"],)).fetchone()
    prior = conn.execute(
        "SELECT COUNT(*) AS n FROM acceptance_attempts WHERE batch_id = ?", (batch["id"],)).fetchone()["n"]
    cursor = conn.execute(
        """INSERT INTO acceptance_attempts
           (batch_id, threshold_set_id, dataset_id, dataset_checksum, status, attempt_no,
            resume_count, publishable, author, created_at)
           VALUES (?, ?, ?, ?, 'queued', ?, 0, 0, ?, ?)""",
        (batch["id"], sealed_row["id"], dataset["id"], dataset["checksum"],
         prior + 1, actor, now()),
    )
    record(conn, actor, "acceptance.queued", cursor.lastrowid,
           batch_id=batch["id"], threshold_set_id=sealed_row["id"],
           attempt_no=prior + 1, dataset_checksum=dataset["checksum"])
    return cursor.lastrowid


def recover_stale(conn):
    """启动/轮询时把租约过期的 running 尝试标记为 interrupted（旧尝试不得发布）。"""
    stale = conn.execute(
        "SELECT * FROM acceptance_attempts WHERE status = 'running'").fetchall()
    recovered = []
    for row in stale:
        if _lease_expired(row["lease_until"]):
            conn.execute(
                "UPDATE acceptance_attempts SET status = 'interrupted', lease_until = NULL, "
                "publishable = 0, finished_at = ? WHERE id = ?",
                (now(), row["id"]))
            record(conn, "worker", "acceptance.interrupted", row["id"],
                   reason="租约过期，进程中断")
            recovered.append(row["id"])
    return recovered


def claim(conn):
    """领取一个待处理尝试；interrupted 尝试续跑，沿用原阈值与数据版本。"""
    recover_stale(conn)
    candidate = conn.execute(
        "SELECT * FROM acceptance_attempts WHERE status IN ('queued', 'interrupted') "
        "ORDER BY id LIMIT 1").fetchone()
    if not candidate:
        return None
    resume_count = candidate["resume_count"] + (1 if candidate["status"] == "interrupted" else 0)
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE acceptance_attempts SET status = 'running', lease_until = ?, resume_count = ? "
        "WHERE id = ?",
        (lease, resume_count, candidate["id"]))
    record(conn, "worker",
           "acceptance.resumed" if candidate["status"] == "interrupted" else "acceptance.started",
           candidate["id"], resume_count=resume_count)
    return conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?",
                        (candidate["id"],)).fetchone()


def run_attempt(conn, attempt):
    """执行验收计算。封存阈值与数据指纹在整个生命周期保持不变。"""
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?",
                           (attempt["dataset_id"],)).fetchone()
    if dataset["checksum"] != attempt["dataset_checksum"]:
        raise DomainError("数据版本与创建尝试时不一致，已中止以保护口径")
    sealed_row = conn.execute("SELECT * FROM threshold_sets WHERE id = ?",
                              (attempt["threshold_set_id"],)).fetchone()
    if not sealed_row:
        raise DomainError("封存阈值已不存在，无法续跑")
    sealed = loads(sealed_row["sealed_json"])
    rows = loads(dataset["rows_json"])
    result = evaluate_acceptance(rows, sealed)
    result["threshold_version"] = sealed_row["version"]
    result["threshold_set_id"] = sealed_row["id"]
    result["dataset_id"] = dataset["id"]
    result["dataset_batch_id"] = dataset["batch_id"]
    result["dataset_checksum"] = dataset["checksum"]
    return result, sealed_row, dataset


def complete(conn, attempt_id, result, export_file):
    """完成尝试：只有绑定当前生效阈值版本的结果才可发布。"""
    attempt = conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?",
                           (attempt_id,)).fetchone()
    sealed_row = conn.execute("SELECT * FROM threshold_sets WHERE id = ?",
                              (attempt["threshold_set_id"],)).fetchone()
    publishable = bool(sealed_row and sealed_row["status"] == "active")
    conn.execute(
        """UPDATE acceptance_attempts
           SET status = 'complete', result_json = ?, export_file = ?, publishable = ?,
               lease_until = NULL, finished_at = ?
           WHERE id = ?""",
        (dumps(result), export_file, 1 if publishable else 0, now(), attempt_id),
    )
    record(conn, "worker", "acceptance.completed", attempt_id,
           passed=result["passed"], publishable=publishable,
           threshold_version=result["threshold_version"])
    if not publishable:
        record(conn, "worker", "acceptance.unpublishable", attempt_id,
               reason="封存阈值已被调参更正取代，旧尝试不得发布")


def fail(conn, attempt_id, error):
    conn.execute(
        "UPDATE acceptance_attempts SET status = 'failed', error = ?, lease_until = NULL, "
        "publishable = 0, finished_at = ? WHERE id = ?",
        (str(error)[:500], now(), attempt_id),
    )
    record(conn, "worker", "acceptance.failed", attempt_id, reason=str(error)[:300])


def resume_interrupted(conn, attempt_id, actor):
    """手动续跑：沿用原阈值和数据版本，新建一次运行记录但不换批不换阈值。"""
    attempt = conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?",
                           (attempt_id,)).fetchone()
    if not attempt:
        raise DomainError("找不到该验收尝试")
    if attempt["status"] != "interrupted":
        raise DomainError("只有中断状态的尝试可以续跑")
    batch = conn.execute("SELECT * FROM acceptance_batches WHERE id = ?",
                         (attempt["batch_id"],)).fetchone()
    new_id = enqueue_by_batch(conn, batch, actor, resume_from=attempt_id)
    return new_id


def enqueue_by_batch(conn, batch, actor, resume_from=None):
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?",
                           (batch["dataset_id"],)).fetchone()
    prior = conn.execute(
        "SELECT MAX(attempt_no) AS n FROM acceptance_attempts WHERE batch_id = ?",
        (batch["id"],)).fetchone()["n"] or 0
    cursor = conn.execute(
        """INSERT INTO acceptance_attempts
           (batch_id, threshold_set_id, dataset_id, dataset_checksum, status, attempt_no,
            resume_count, publishable, author, created_at)
           VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?)""",
        (batch["id"], batch["threshold_set_id"], dataset["id"], dataset["checksum"],
         prior + 1, 1 if resume_from else 0, actor, now()),
    )
    record(conn, actor, "acceptance.queued", cursor.lastrowid,
           batch_id=batch["id"], resume_from=resume_from,
           threshold_set_id=batch["threshold_set_id"],
           dataset_checksum=dataset["checksum"])
    return cursor.lastrowid


def list_attempts(conn):
    return conn.execute(
        """SELECT a.*, d.batch_id AS dataset_batch_id, ts.version AS threshold_version,
                  b.threshold_set_id AS bound_set_id
           FROM acceptance_attempts a
           JOIN datasets d ON d.id = a.dataset_id
           JOIN threshold_sets ts ON ts.id = a.threshold_set_id
           JOIN acceptance_batches b ON b.id = a.batch_id
           ORDER BY a.id DESC""").fetchall()


def get_attempt(conn, attempt_id):
    row = conn.execute(
        """SELECT a.*, d.batch_id AS dataset_batch_id, ts.version AS threshold_version
           FROM acceptance_attempts a
           JOIN datasets d ON d.id = a.dataset_id
           JOIN threshold_sets ts ON ts.id = a.threshold_set_id
           WHERE a.id = ?""", (attempt_id,)).fetchone()
    if not row:
        raise DomainError("找不到该验收尝试")
    return row


def list_batches(conn):
    return conn.execute(
        """SELECT b.*, d.batch_id, d.row_count, d.checksum, ts.version AS threshold_version,
                  ts.status AS threshold_status
           FROM acceptance_batches b
           JOIN datasets d ON d.id = b.dataset_id
           JOIN threshold_sets ts ON ts.id = b.threshold_set_id
           ORDER BY b.id DESC""").fetchall()
