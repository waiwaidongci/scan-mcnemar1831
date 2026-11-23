#!/usr/bin/env python3
"""入口脚本：python -I run_server.py。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging

from credit_release.config import HOST, PORT
from credit_release.httpapp import create_server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server()
    logging.getLogger("credit_release.http").info("置信分放行服务监听 http://%s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
