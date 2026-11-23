"""数据批次服务：导入校验、批号/票号互斥、封存前禁止验收批。"""

from .. import domain
from ..config import DATASET_ROLES
from ..db import dumps
from ..domain import DomainError, checksum, parse_csv, validate_batch_integrity
from ..timeutil import now


def import_dataset(conn, role, name, filename, csv_text, actor):
    if role not in DATASET_ROLES:
        raise DomainError("批次角色须为 tuning 或 acceptance")
    rows = parse_csv(csv_text)
    batch_id = validate_batch_integrity(rows)

    clash = conn.execute("SELECT id, role FROM datasets WHERE batch_id = ?", (batch_id,)).fetchone()
    if clash:
        raise DomainError(
            f"批号 {batch_id} 已被{'调参批' if clash['role'] == 'tuning' else '验收批'}使用，"
            "调参批与验收批不得共用批号")

    if role == "acceptance":
        sealed = conn.execute("SELECT 1 FROM threshold_sets WHERE status = 'active' LIMIT 1").fetchone()
        if not sealed:
            raise DomainError("尚未封存任何阈值：验收批只能在首版阈值封存后导入，以防提前揭晓")

    signature = checksum(rows)
    duplicate = conn.execute(
        "SELECT id FROM datasets WHERE checksum = ? AND role = ?", (signature, role)).fetchone()
    if duplicate:
        raise DomainError("相同内容的数据批次已导入，请勿重复上传")

    for row in rows:
        occupied = conn.execute("SELECT dataset_id FROM ticket_index WHERE ticket_id = ?",
                                (row["ticket_id"],)).fetchone()
        if occupied:
            raise DomainError(
                f"票号 {row['ticket_id']} 已在其他批次中使用，调参批与验收批不得共用票号")

    cursor = conn.execute(
        """INSERT INTO datasets (role, batch_id, name, filename, checksum, rows_json,
                                 row_count, revealed, author, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (role, batch_id, name[:120], filename[:160], signature, dumps(rows),
         len(rows), actor, now()),
    )
    dataset_id = cursor.lastrowid
    conn.executemany("INSERT INTO ticket_index (ticket_id, dataset_id) VALUES (?, ?)",
                     [(row["ticket_id"], dataset_id) for row in rows])
    return dataset_id, batch_id, signature, len(rows)


def get_dataset(conn, dataset_id):
    row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not row:
        raise DomainError("找不到该数据批次")
    return row


def list_datasets(conn):
    return conn.execute("SELECT * FROM datasets ORDER BY id DESC").fetchall()
