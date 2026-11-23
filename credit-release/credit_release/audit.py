"""审计记录。"""

from .db import dumps
from .timeutil import now


def record(conn, actor, event, target=None, **info):
    conn.execute(
        "INSERT INTO audit_events (actor, event, target, info_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor, event, str(target) if target is not None else None,
         dumps(info) if info else None, now()),
    )
