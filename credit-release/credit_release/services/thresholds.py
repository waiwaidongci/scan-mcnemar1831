"""阈值封存服务：只允许调参批；同批只认一套封存阈值；更正封存后旧版作废。"""

from .. import domain
from ..audit import record
from ..db import dumps, loads
from ..domain import DomainError, build_candidate_preview, normalize_caps, select_thresholds
from ..timeutil import now


def preview(conn, dataset_id, caps):
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not dataset:
        raise DomainError("找不到该数据批次")
    if dataset["role"] != "tuning":
        raise DomainError("阈值只能在调参批上拟合，验收批不得参与调参")
    rows = loads(dataset["rows_json"])
    caps = normalize_caps(caps)
    preview, chosen = build_candidate_preview(rows, caps)
    return {
        "dataset_id": dataset_id,
        "batch_id": dataset["batch_id"],
        "caps": caps,
        "preview": preview,
        "chosen": chosen,
    }


def active_set(conn):
    return conn.execute(
        "SELECT * FROM threshold_sets WHERE status = 'active' ORDER BY version DESC LIMIT 1"
    ).fetchone()


def seal(conn, dataset_id, caps, actor):
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not dataset:
        raise DomainError("找不到该数据批次")
    if dataset["role"] != "tuning":
        raise DomainError("阈值只能在调参批上封存，验收批不得参与调参")
    caps = normalize_caps(caps)
    rows = loads(dataset["rows_json"])
    sealed = select_thresholds(rows, caps)

    previous = active_set(conn)
    previous_version = previous["version"] if previous else 0
    if previous:
        conn.execute(
            "UPDATE threshold_sets SET status = 'superseded', superseded_at = ? WHERE id = ?",
            (now(), previous["id"]),
        )
        # 旧封存版本上的任何尝试立即失去发布资格（含已完成尝试），旧尝试不得发布。
        stale = conn.execute(
            "SELECT id FROM acceptance_attempts WHERE threshold_set_id = ? AND publishable = 1",
            (previous["id"],)).fetchall()
        if stale:
            conn.execute(
                "UPDATE acceptance_attempts SET publishable = 0 WHERE threshold_set_id = ?",
                (previous["id"],))
            for item in stale:
                record(conn, actor, "acceptance.unpublishable", item["id"],
                       reason="封存阈值已被调参更正取代，旧尝试不得发布",
                       superseded_set=previous["id"], new_version=previous_version + 1)
    cursor = conn.execute(
        """INSERT INTO threshold_sets
           (version, status, dataset_id, dataset_checksum, caps_json, sealed_json, author, created_at)
           VALUES (?, 'active', ?, ?, ?, ?, ?, ?)""",
        (previous_version + 1, dataset_id, dataset["checksum"],
         dumps(caps), dumps(sealed), actor, now()),
    )
    new_id = cursor.lastrowid
    record(conn, actor, "threshold.sealed", new_id,
           version=previous_version + 1, dataset_id=dataset_id,
           batch_id=dataset["batch_id"], caps=caps,
           supersedes=previous["id"] if previous else None,
           blocked_types=[t for t, cfg in sealed.items() if cfg["blocked"]])
    return new_id, previous_version + 1, sealed


def list_sets(conn):
    return conn.execute("SELECT * FROM threshold_sets ORDER BY version DESC").fetchall()


def set_view(row):
    return {
        "id": row["id"],
        "version": row["version"],
        "status": row["status"],
        "dataset_id": row["dataset_id"],
        "dataset_checksum": row["dataset_checksum"],
        "caps": loads(row["caps_json"]),
        "sealed": loads(row["sealed_json"]),
        "author": row["author"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
    }
