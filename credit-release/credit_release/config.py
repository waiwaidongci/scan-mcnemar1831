"""运行配置：全部来自环境变量，带本机演示默认值。"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CR_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
ARTIFACT_DIR = DATA_DIR / "artifacts"
DB_PATH = DATA_DIR / "credit_release.db"
SECRET_PATH = DATA_DIR / "secret.key"

HOST = os.environ.get("CR_HOST", "127.0.0.1")
PORT = int(os.environ.get("CR_PORT", "18372"))
SEED_DEMO = os.environ.get("CR_SEED_DEMO", "true").lower() == "true"
LEASE_SECONDS = int(os.environ.get("CR_LEASE_SECONDS", "30"))
MAX_ROWS = 5000
SCORE_MIN, SCORE_MAX = 0.0, 100.0
DEFAULT_CAP = 2

TICKET_TYPES = ["增值税发票", "零售小票"]
DATASET_ROLES = ["tuning", "acceptance"]
ROLE_LABEL = {"tuning": "调参批", "acceptance": "验收批"}

CSV_FIELDS = [
    ("batch_id", "批号"),
    ("ticket_id", "票号"),
    ("ticket_type", "票种"),
    ("score_b", "B 置信分"),
    ("a_release", "A 是否放行"),
    ("manual_qualified", "人工真值是否合格"),
]

BOOL_TEXT = {
    "true": True, "1": True, "是": True, "y": True, "yes": True,
    "false": False, "0": False, "否": False, "n": False, "no": False,
}


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
