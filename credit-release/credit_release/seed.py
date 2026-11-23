"""演示数据：两票种、调参/验收/复验三个互斥批次，确定性可复现。"""

import random

from .config import TICKET_TYPES
from .db import dumps
from .domain import checksum
from .security import hash_password
from .timeutil import now

DEMO_USERS = [
    ("admin", "admin", "admin-demo-2611"),
    ("analyst", "analyst", "analyst-demo-2611"),
    ("viewer", "viewer", "viewer-demo-2611"),
]


def _make_rows(batch_id, ticket_type, n, seed, qualified_base, noise, a_recall):
    rng = random.Random(seed)
    rows = []
    for index in range(1, n + 1):
        qualified = rng.random() < (qualified_base + rng.uniform(-noise, noise))
        # 合格票分数整体偏高、不合格票偏低，仍保留重叠以制造错放权衡。
        center = 88 if qualified else 42
        score = round(min(100.0, max(0.0, rng.gauss(center, 11))), 4)
        # A 引擎：对合格票按召回率放行，同时有一定误放。
        if qualified:
            a_release = rng.random() < a_recall
        else:
            a_release = rng.random() < 0.12
        rows.append({
            "batch_id": batch_id,
            "ticket_id": f"{batch_id}-{ticket_type[:2]}-{index:03d}",
            "ticket_type": ticket_type,
            "score_b": score,
            "a_release": a_release,
            "manual_qualified": qualified,
        })
    return rows


def demo_batches():
    """返回 (调参批, 验收批, 复验新批) 三个批次，批号/票号天然互斥。"""
    plans = [
        ("TUNE-20261120", "tuning", 60, 0.78, 0.06, 0.70),
        ("ACCT-20261121", "acceptance", 50, 0.80, 0.06, 0.66),
        ("ACCT-20261122", "acceptance", 46, 0.82, 0.05, 0.64),
    ]
    batches = []
    for seq, (batch_id, role, per_type, base, noise, recall) in enumerate(plans, 1):
        rows = []
        for t_index, ticket_type in enumerate(TICKET_TYPES):
            rows += _make_rows(batch_id, ticket_type, per_type,
                               seed=20261120 + seq * 17 + t_index * 101,
                               qualified_base=base, noise=noise, a_recall=recall)
        batches.append({"batch_id": batch_id, "role": role, "rows": rows,
                        "name": f"演示{ '调参批' if role == 'tuning' else '验收批'} {batch_id}"})
    return batches


def seed(conn):
    created_users = 0
    for username, role, password in DEMO_USERS:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (username, role, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, role, hash_password(password), now()))
            created_users += 1

    created_datasets = []
    for batch in demo_batches():
        if conn.execute("SELECT 1 FROM datasets WHERE batch_id = ?",
                        (batch["batch_id"],)).fetchone():
            continue
        signature = checksum(batch["rows"])
        cursor = conn.execute(
            """INSERT INTO datasets (role, batch_id, name, filename, checksum, rows_json,
                                     row_count, revealed, author, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'seed', ?)""",
            (batch["role"], batch["batch_id"], batch["name"],
             f"{batch['batch_id']}.csv", signature, dumps(batch["rows"]),
             len(batch["rows"]), now()),
        )
        conn.executemany("INSERT INTO ticket_index (ticket_id, dataset_id) VALUES (?, ?)",
                         [(row["ticket_id"], cursor.lastrowid) for row in batch["rows"]])
        created_datasets.append(batch["batch_id"])
    return {"users": created_users, "datasets": created_datasets}
