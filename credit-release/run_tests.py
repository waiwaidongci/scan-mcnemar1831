#!/usr/bin/env python3
"""一键运行全部测试：python3 -I run_tests.py"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ["tests/test_domain.py", "tests/test_services.py", "tests/test_http.py"]

failures = 0
for test_file in TESTS:
    print(f"\n===== {test_file} =====")
    result = subprocess.run(
        [sys.executable, "-I", test_file],
        cwd=ROOT,
        env={
            **__import__("os").environ,
            "CR_DATA_DIR": tempfile.mkdtemp(prefix=f"cr-{Path(test_file).stem}-"),
            "CR_SEED_DEMO": "false",
        },
    )
    if result.returncode != 0:
        failures += 1

print()
if failures:
    print(f"有 {failures} 个测试文件失败")
    sys.exit(1)
print("全部测试通过")
