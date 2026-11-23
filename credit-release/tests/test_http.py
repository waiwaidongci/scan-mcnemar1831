"""HTTP 端到端测试：真实启动服务，覆盖鉴权、封存、验收、导出全链路。"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cr-http-")
os.environ["CR_DATA_DIR"] = _TMP
os.environ["CR_SEED_DEMO"] = "true"
os.environ["CR_PORT"] = "0"  # 由测试改用空闲端口
os.environ["CR_HOST"] = "127.0.0.1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from http.server import ThreadingHTTPServer  # noqa: E402

from credit_release import db  # noqa: E402
from credit_release.httpapp import Handler  # noqa: E402


def request(server, path, token=None, body=None, expect_error=False):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if "json" in response.headers.get("Content-Type", "")
                                     else raw)
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read())
        if expect_error:
            return error.code, payload
        raise AssertionError(f"{path} 返回 {error.code}: {payload}") from None


def wait_for(server, token, attempt_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = request(server, f"/api/acceptance/{attempt_id}", token)
        if payload["status"] in ("complete", "failed", "interrupted"):
            return payload
        time.sleep(0.1)
    raise AssertionError("验收尝试超时未结束")


class HttpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        from credit_release.seed import seed
        with db.tx() as conn:
            seed(conn)
        from credit_release.services import acceptance
        with db.tx() as conn:
            acceptance.recover_stale(conn)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def login(self, username, password):
        status, payload = request(self.server, "/api/auth/login",
                                  body={"username": username, "password": password})
        self.assertEqual(status, 200)
        return payload["token"]

    def test_00_health_and_spec(self):
        status, payload = request(self.server, "/healthz")
        self.assertEqual(payload["status"], "ok")
        status, payload = request(self.server, "/api/spec")
        self.assertEqual(len(payload["ticket_types"]), 2)

    def test_01_auth_enforced(self):
        status, payload = request(self.server, "/api/datasets", expect_error=True)
        self.assertEqual(status, 401)
        status, payload = request(self.server, "/api/auth/login",
                                  body={"username": "admin", "password": "wrong"}, expect_error=True)
        self.assertEqual(status, 401)

    def test_02_role_permissions(self):
        viewer = self.login("viewer", "viewer-demo-2611")
        analyst = self.login("analyst", "analyst-demo-2611")
        admin = self.login("admin", "admin-demo-2611")
        type(self).tokens = {"viewer": viewer, "analyst": analyst, "admin": admin}

        # viewer 不能导入
        status, _ = request(self.server, "/api/datasets", token=viewer,
                            body={"role": "tuning", "name": "x", "csv_text": "bad"}, expect_error=True)
        self.assertEqual(status, 403)
        # analyst 不能封存
        status, _ = request(self.server, "/api/thresholds/seal", token=analyst,
                            body={"dataset_id": 1}, expect_error=True)
        self.assertEqual(status, 403)
        # viewer 不能看审计
        status, _ = request(self.server, "/api/audit", token=viewer, expect_error=True)
        self.assertEqual(status, 403)

    def test_03_import_validation_and_seal_flow(self):
        token = self.tokens["analyst"]
        # 错误 CSV
        status, payload = request(self.server, "/api/datasets", token=token,
                                  body={"role": "tuning", "name": "错误文件",
                                        "csv_text": "批号,票号\nB,T"}, expect_error=True)
        self.assertEqual(status, 400)

        # 导入新调参批（演示种子已占用 TUNE 演示批号，这里用独立批号）
        csv_text = self._tuning_csv("TUNE-HT-1")
        status, payload = request(self.server, "/api/datasets", token=token,
                                  body={"role": "tuning", "name": "HTTP 调参批",
                                        "filename": "t1.csv", "csv_text": csv_text})
        self.assertEqual(status, 201)
        tuning_id = payload["id"]
        type(self).tuning_id = tuning_id

        # 封存前导入验收批被拒
        from credit_release.seed import demo_batches
        existing_batches = {b["batch_id"] for b in demo_batches()}
        self.assertNotIn("ACCT-HT-1", existing_batches)
        acct_csv = self._acceptance_csv("ACCT-HT-1", beat_a=True)
        status, payload = request(self.server, "/api/datasets", token=token,
                                  body={"role": "acceptance", "name": "HTTP 验收批",
                                        "csv_text": acct_csv}, expect_error=True)
        self.assertEqual(status, 400)
        self.assertIn("尚未封存", payload["detail"])

        # analyst 先预览，admin 封存
        caps = {"增值税发票": 1, "零售小票": 1}
        status, preview = request(self.server, "/api/thresholds/preview", token=token,
                                  body={"dataset_id": tuning_id, "caps": caps})
        self.assertEqual(status, 200)
        self.assertTrue(any(c["selected"] for c in preview["preview"]["增值税发票"]))

        status, sealed = request(self.server, "/api/thresholds/seal", token=self.tokens["admin"],
                                 body={"dataset_id": tuning_id, "caps": caps})
        self.assertEqual(status, 201)
        self.assertEqual(sealed["version"], 1)
        type(self).sealed_payload = sealed

    def test_04_acceptance_run_poll_and_export(self):
        token = self.tokens["analyst"]
        # 封存后再导入验收批
        status, payload = request(self.server, "/api/datasets", token=token,
                                  body={"role": "acceptance", "name": "HTTP 验收批",
                                        "csv_text": self._acceptance_csv("ACCT-HT-1", beat_a=True)})
        self.assertEqual(status, 201)
        acct_id = payload["id"]

        # 并发两个尝试
        status, first = request(self.server, "/api/acceptance/run", token=token,
                                body={"dataset_id": acct_id})
        status, second = request(self.server, "/api/acceptance/run", token=token,
                                 body={"dataset_id": acct_id})
        self.assertEqual(first["status"], "queued")

        result_first = wait_for(self.server, token, first["attempt_id"])
        result_second = wait_for(self.server, token, second["attempt_id"])
        self.assertTrue(result_first["result"]["passed"])
        self.assertTrue(result_first["publishable"])
        self.assertEqual(result_first["threshold_version"], 1)
        # 同批两套尝试使用同一封存阈值
        self.assertEqual(result_first["threshold_set_id"], result_second["threshold_set_id"])
        type(self).attempt_id = first["attempt_id"]

        # 页面与导出同版：导出里包含同样的阈值版本、批号、合格放行与错放数
        status, content = request(self.server,
                                  f"/api/exports/{self.attempt_id}/export", token)
        text = content.decode("utf-8")
        self.assertIn("封存阈值版本", text)
        self.assertIn("ACCT-HT-1", text)
        for item in result_first["result"]["per_type"]:
            self.assertIn(str(item["b_released_qualified"]), text)
            self.assertIn(str(item["b_wrong_releases"]), text)

    def test_05_reseal_requires_new_batch_and_old_attempt_unpublishable(self):
        admin, analyst = self.tokens["admin"], self.tokens["analyst"]
        # admin 用更严上限封存 v2
        status, sealed = request(self.server, "/api/thresholds/seal", token=admin,
                                 body={"dataset_id": self.tuning_id,
                                       "caps": {"增值税发票": 0, "零售小票": 0}})
        self.assertEqual(sealed["version"], 2)

        # 旧尝试变为不可发布，导出被拒
        status, payload = request(self.server,
                                  f"/api/acceptance/{self.attempt_id}", analyst)
        self.assertFalse(payload["publishable"])
        status, payload = request(self.server,
                                  f"/api/exports/{self.attempt_id}/export", analyst,
                                  expect_error=True)
        self.assertEqual(status, 409)
        self.assertIn("不得发布", payload["detail"])

        # 已揭晓批次再跑被拒
        from credit_release.seed import demo_batches
        acct1 = next(b for b in demo_batches() if b["batch_id"] == "ACCT-HT-1") if False else None
        status, datasets = request(self.server, "/api/datasets", analyst)
        revealed_id = next(d["id"] for d in datasets if d["batch_id"] == "ACCT-HT-1")
        status, payload = request(self.server, "/api/acceptance/run", token=analyst,
                                  body={"dataset_id": revealed_id}, expect_error=True)
        self.assertEqual(status, 400)
        self.assertIn("尚未揭晓的新验收批", payload["detail"])

        # 导入未揭晓新批并验收，绑定 v2
        status, payload = request(self.server, "/api/datasets", token=analyst,
                                  body={"role": "acceptance", "name": "HTTP 复验批",
                                        "csv_text": self._acceptance_csv("ACCT-HT-2", beat_a=True)})
        new_acct = payload["id"]
        status, run = request(self.server, "/api/acceptance/run", token=analyst,
                              body={"dataset_id": new_acct})
        outcome = wait_for(self.server, analyst, run["attempt_id"])
        self.assertEqual(outcome["threshold_version"], 2)
        self.assertTrue(outcome["publishable"])

    def test_06_audit_visible_to_admin(self):
        status, payload = request(self.server, "/api/audit?limit=50",
                                  token=self.tokens["admin"])
        events = {item["event"] for item in payload}
        self.assertIn("threshold.sealed", events)
        self.assertIn("acceptance.completed", events)
        self.assertIn("acceptance.unpublishable", events)
        self.assertIn("auth.login", events)

    # ---- 构造 CSV ----
    def _tuning_csv(self, batch_id):
        lines = ["批号,票号,票种,B 置信分,A 是否放行,人工真值是否合格"]
        plan = {
            "增值税发票": [(99, "是", "是"), (95, "是", "是"), (90, "否", "是"),
                          (88, "否", "否"), (80, "否", "是"), (72, "否", "是"),
                          (60, "否", "否"), (50, "否", "是")],
            "零售小票": [(97, "是", "是"), (92, "否", "是"), (85, "否", "是"),
                        (78, "否", "是"), (65, "否", "否"), (55, "否", "是"),
                        (40, "否", "否"), (30, "否", "否")],
        }
        for ticket_type, entries in plan.items():
            for index, (score, a_release, qualified) in enumerate(entries, 1):
                lines.append(f"{batch_id},{batch_id}-{ticket_type[:2]}-{index:03d},"
                             f"{ticket_type},{score},{a_release},{qualified}")
        return "\n".join(lines)

    def _acceptance_csv(self, batch_id, beat_a=True):
        lines = ["批号,票号,票种,B 置信分,A 是否放行,人工真值是否合格"]
        plan = {
            "增值税发票": [(96, "否", "是"), (91, "否", "是"), (87, "否", "是"),
                          (70, "否", "是"), (55, "是", "否"), (40, "否", "否")],
            "零售小票": [(98, "否", "是"), (90, "否", "是"), (82, "是", "是"),
                        (60, "否", "否"), (45, "否", "否"), (33, "否", "是")],
        }
        for ticket_type, entries in plan.items():
            for index, (score, a_release, qualified) in enumerate(entries, 1):
                lines.append(f"{batch_id},{batch_id}-{ticket_type[:2]}-{index:03d},"
                             f"{ticket_type},{score},{a_release},{qualified}")
        return "\n".join(lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
