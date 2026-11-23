#!/usr/bin/env python3
"""独立 worker 入口（默认服务已内置 worker 线程，此脚本用于多进程部署）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from credit_release.worker import serve_forever

if __name__ == "__main__":
    serve_forever()
