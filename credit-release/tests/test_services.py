"""服务层端到端测试：隔离临时数据目录，覆盖封存、并发、中断续跑、更正换批全流程。"""

import csv
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cr-test-")
os.environ["CR_DATA_DIR"] = _TMP
os.environ["CR_SEED_DEMO"] = "false"
os.environ["CR_LEASE_SECONDS"] = "3600"  # 测试内手动控制租约过期

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credit_release import db  # noqa: E402
from credit_release.domain import TICKET_TYPES  # noqa: E402
from credit_release.security import hash_password  # noqa: E402
from credit_release.services import acceptance, datasets as dataset_service  # noqa: E402
from credit_release.services.reports import render_csv  # noqa: E402
from credit_release.services.thresholds import active_set, list_sets, preview, seal  # noqa: E402
from credit_release.timeutil import now  # noqa: E402


def make_csv(batch_id, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["批号", "票号", "票种", "B 置信分", "A 是否放行", "人工真值是否合格"])
    for r in rows:
        writer.writerow([batch_id, r[0], r[1], r[2], "是" if r[3] else "否", "是" if r[4] else "否"])
    return buffer.getvalue()


def balanced_rows(prefix, scores_by_type, a_by_type, qualified_by_type):
    rows = []
    for ticket_type in TICKET_TYPES:
        for index, (score, a_release, qualified) in enumerate(
                zip(scores_by_type[ticket_type], a_by_type[ticket_type],
                    qualified_by_type[ticket_type]), 1):
            rows.append((f"{prefix}-{ticket_type[:2]}-{index:03d}", ticket_type,
                         score, a_release, qualified))
    return rows


def tuning_rows(batch_id):
    # 增值税发票：高分错票用于检验 cap；零售小票：分数与真值较整齐
    data = {
        "增值税发票": (
            [99, 95, 90, 88, 80, 72, 60, 50],
            [True, True, True, False, False, False, False, False],
            [True, True, True, False, True, True, False, True],
        ),
        "零售小票": (
            [97, 92, 85, 78, 65, 55, 40, 30],
            [True, True, False, False, False, False, False, False],
            [True, True, True, True, False, True, False, False],
        ),
    }
    return make_csv(batch_id, balanced_rows(
        batch_id, {t: v[0] for t, v in data.items()},
        {t: v[1] for t, v in data.items()},
        {t: v[2] for t, v in data.items()}))


def acceptance_rows(batch_id, beat_a=True):
    # 多数高分票为合格且 A 未放行 → B 在阈值下能超过 A
    if beat_a:
        data = {
            "增值税发票": (
                [96, 91, 87, 70, 55, 40],
                [False, False, False, False, True, False],
                [True, True, True, True, False, False],
            ),
            "零售小票": (
                [98, 90, 82, 60, 45, 33],
                [False, False, True, False, False, False],
                [True, True, True, False, False, True],
            ),
        }
    else:
        data = {
            "增值税发票": (
                [96, 91, 87],
                [True, True, True],
                [True, True, True],
            ),
            "零售小票": (
                [98, 90, 82],
                [True, True, True],
                [True, True, True],
            ),
        }
    return make_csv(batch_id, balanced_rows(
        batch_id, {t: v[0] for t, v in data.items()},
        {t: v[1] for t, v in data.items()},
        {t: v[2] for t, v in data.items()}))


class ServiceFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        with db.tx() as conn:
            conn.execute("DELETE FROM audit_events")
            conn.execute("DELETE FROM acceptance_attempts")
            conn.execute("DELETE FROM acceptance_batches")
            conn.execute("DELETE FROM threshold_sets")
            conn.execute("DELETE FROM ticket_index")
            conn.execute("DELETE FROM datasets")
            if not conn.execute("SELECT 1 FROM users WHERE username = 'analyst'").fetchone():
                conn.execute(
                    "INSERT INTO users (username, role, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    ("analyst", "analyst", hash_password("pw"), now()))

    def _import(self, role, batch_id, csv_text, name=None):
        with db.tx() as conn:
            return dataset_service.import_dataset(
                conn, role, name or f"批次 {batch_id}", f"{batch_id}.csv", csv_text, "analyst")

    def test_01_acceptance_blocked_before_seal(self):
        with self.assertRaises(Exception) as caught:
            self._import("acceptance", "ACCT-EARLY", acceptance_rows("ACCT-EARLY"))
        self.assertIn("尚未封存", str(caught.exception))

    def test_02_import_tuning_and_validation(self):
        dataset_id, batch_id, checksum, count = self._import(
            "tuning", "TUNE-1", tuning_rows("TUNE-1"))
        self.assertEqual(batch_id, "TUNE-1")
        self.assertEqual(count, 16)
        type(self).tuning_id = dataset_id

        # 批号不得与调参批共用（tuning 角色不受"未封存"限制，直接命中批号冲突）
        with self.assertRaises(Exception) as caught:
            self._import("tuning", "TUNE-1", acceptance_rows("TUNE-1"))
        self.assertIn("批号", str(caught.exception))

        # 票号不得跨批共用：新批号但沿用 TUNE-1 的票号
        lines = tuning_rows("TUNE-1").splitlines()
        reused_lines = [lines[0]] + ["TUNE-2," + line.split(",", 1)[1] for line in lines[1:]]
        reused_csv = "\n".join(reused_lines)
        with self.assertRaises(Exception) as caught:
            self._import("tuning", "TUNE-2", reused_csv)
        self.assertIn("票号", str(caught.exception))

    def test_03_preview_and_seal_v1(self):
        caps = {t: 1 for t in TICKET_TYPES}
        with db.read_conn() as conn:
            preview_data = preview(conn, self.tuning_id, caps)
            for ticket_type in TICKET_TYPES:
                selected = [c for c in preview_data["preview"][ticket_type] if c["selected"]]
                self.assertEqual(len(selected), 1)
            new_id, version, sealed = seal(conn, self.tuning_id, caps, "admin")
        self.assertEqual(version, 1)
        for ticket_type in TICKET_TYPES:
            self.assertIsNotNone(sealed[ticket_type]["threshold"])
        type(self).set_v1_id = new_id

    def test_04_seal_only_on_tuning(self):
        self._import("acceptance", "ACCT-1", acceptance_rows("ACCT-1"))
        with db.read_conn() as conn:
            acct = conn.execute("SELECT id FROM datasets WHERE batch_id = 'ACCT-1'").fetchone()
        with self.assertRaises(Exception):
            with db.tx() as conn:
                seal(conn, acct["id"], {t: 1 for t in TICKET_TYPES}, "admin")

    def test_05_concurrent_acceptance_uses_one_threshold_set(self):
        with db.read_conn() as conn:
            acct_id = conn.execute(
                "SELECT id FROM datasets WHERE batch_id = 'ACCT-1'").fetchone()["id"]
        with db.tx() as conn:
            first = acceptance.enqueue(conn, acct_id, "analyst")
            second = acceptance.enqueue(conn, acct_id, "analyst")
        self.assertNotEqual(first, second)
        with db.read_conn() as conn:
            sets = conn.execute(
                "SELECT DISTINCT threshold_set_id FROM acceptance_attempts").fetchall()
        self.assertEqual(len(sets), 1)
        type(self).acct_id = acct_id
        type(self).attempt_ids = [first, second]

    def test_06_worker_completes_attempts(self):
        for _ in range(5):
            from credit_release.worker import tick
            if not tick():
                break
        with db.read_conn() as conn:
            for attempt_id in self.attempt_ids:
                row = acceptance.get_attempt(conn, attempt_id)
                self.assertEqual(row["status"], "complete")
                self.assertTrue(row["publishable"])
                result = db.loads(row["result_json"])
                self.assertTrue(result["all_within_cap"])
                self.assertTrue(result["b_beats_a"])
                self.assertTrue(result["passed"])
                self.assertEqual(result["threshold_version"], 1)

    def test_07_export_same_version_as_page(self):
        with db.read_conn() as conn:
            first_attempt = acceptance.get_attempt(conn, self.attempt_ids[0])
            text = render_csv(first_attempt)
            result = db.loads(first_attempt["result_json"])
        self.assertIn("封存阈值版本", text)
        self.assertIn("ACCT-1", text)
        for item in result["per_type"]:
            self.assertIn(item["ticket_type"], text)
            self.assertIn(str(item["b_released_qualified"]), text)
            self.assertIn(str(item["b_wrong_releases"]), text)

    def test_08_interrupted_resume_keeps_threshold_and_data(self):
        # 制造一个中断尝试
        with db.tx() as conn:
            new_id = acceptance.enqueue(conn, self.acct_id, "analyst")
            claimed = acceptance.claim(conn)
            self.assertEqual(claimed["id"], new_id)
            expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
            conn.execute("UPDATE acceptance_attempts SET lease_until = ? WHERE id = ?",
                         (expired, new_id))
        with db.tx() as conn:
            recovered = acceptance.recover_stale(conn)
        self.assertIn(new_id, recovered)
        with db.tx() as conn:
            resumed_id = acceptance.resume_interrupted(conn, new_id, "analyst")
        with db.read_conn() as conn:
            old = conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?", (new_id,)).fetchone()
            new = conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?", (resumed_id,)).fetchone()
            self.assertEqual(old["threshold_set_id"], new["threshold_set_id"])
            self.assertEqual(old["dataset_checksum"], new["dataset_checksum"])
            self.assertEqual(old["status"], "interrupted")
            self.assertEqual(new["resume_count"], 1)
        from credit_release.worker import tick
        for _ in range(5):
            if not tick():
                break
        with db.read_conn() as conn:
            done = conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?",
                                (resumed_id,)).fetchone()
            self.assertEqual(done["status"], "complete")

    def test_09_reseal_supersedes_and_revealed_batch_rejected(self):
        # 在调参批上以更严上限封存 v2
        with db.tx() as conn:
            _id, version, _sealed = seal(conn, self.tuning_id,
                                         {t: 0 for t in TICKET_TYPES}, "admin")
        self.assertEqual(version, 2)
        with db.read_conn() as conn:
            sets = list_sets(conn)
            self.assertEqual(sets[0]["status"], "active")
            self.assertEqual(sets[1]["status"], "superseded")

            # 已揭晓的 ACCT-1 再发起验收必须被拒绝
            with self.assertRaises(Exception) as caught:
                acceptance.enqueue(conn, self.acct_id, "analyst")
            self.assertIn("尚未揭晓的新验收批", str(caught.exception))

            # v1 上完成的旧尝试不得发布
            old_complete = conn.execute(
                "SELECT * FROM acceptance_attempts WHERE id = ?",
                (self.attempt_ids[0],)).fetchone()
            self.assertEqual(old_complete["publishable"], 0)

    def test_10_new_unrevealed_batch_binds_v2_and_passes_or_fails_on_merit(self):
        self._import("acceptance", "ACCT-2", acceptance_rows("ACCT-2"))
        with db.read_conn() as conn:
            acct2 = conn.execute("SELECT id FROM datasets WHERE batch_id = 'ACCT-2'").fetchone()
        with db.tx() as conn:
            attempt_id = acceptance.enqueue(conn, acct2["id"], "analyst")
            bound = conn.execute(
                "SELECT threshold_set_id FROM acceptance_batches WHERE dataset_id = ?",
                (acct2["id"],)).fetchone()
            self.assertEqual(bound["threshold_set_id"], active_set(conn)["id"])
        from credit_release.worker import tick
        for _ in range(5):
            if not tick():
                break
        with db.read_conn() as conn:
            row = acceptance.get_attempt(conn, attempt_id)
            self.assertEqual(row["status"], "complete")
            self.assertTrue(row["publishable"])
            self.assertEqual(db.loads(row["result_json"])["threshold_version"], 2)

    def test_11_audit_trail_written(self):
        with db.read_conn() as conn:
            events = [r["event"] for r in conn.execute(
                "SELECT event FROM audit_events ORDER BY id").fetchall()]
        for expected in ("threshold.sealed", "acceptance.bound", "acceptance.queued",
                         "acceptance.completed", "acceptance.interrupted",
                         "acceptance.unpublishable"):
            self.assertIn(expected, events, f"缺少审计事件 {expected}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
