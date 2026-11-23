"""后台验收 worker：单线程轮询租约，中断后续跑沿用原阈值与数据版本。"""

import logging
import time

from . import db
from .services import acceptance
from .services.reports import write_export

log = logging.getLogger("credit_release.worker")


def tick():
    with db.tx() as conn:
        attempt = acceptance.claim(conn)
        if attempt is None:
            return False
        attempt_id = attempt["id"]
    try:
        with db.tx() as conn:
            fresh = conn.execute("SELECT * FROM acceptance_attempts WHERE id = ?",
                                 (attempt_id,)).fetchone()
            if fresh is None or fresh["status"] != "running":
                return True
            result, _sealed, _dataset = acceptance.run_attempt(conn, fresh)
            export_file = write_export({**fresh, "result_json": db.dumps(result)})
            acceptance.complete(conn, attempt_id, result, export_file)
    except Exception as error:  # 业务/系统错误均落库，避免静默丢任务
        log.exception("验收尝试 %s 失败", attempt_id)
        with db.tx() as conn:
            acceptance.fail(conn, attempt_id, error)
    return True


def serve_forever(poll_interval=1.0):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    log.info("验收 worker 已启动")
    while True:
        try:
            worked = tick()
        except Exception:
            log.exception("worker 轮询异常")
            worked = False
        time.sleep(0.1 if worked else poll_interval)


if __name__ == "__main__":
    serve_forever()
