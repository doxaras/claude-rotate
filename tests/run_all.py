"""Run every test file in this directory: python3 tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

failed = []
for f in sorted(Path(__file__).parent.glob("test_*.py")):
    print(f"=== {f.name} ===")
    if subprocess.run([sys.executable, str(f)]).returncode != 0:
        failed.append(f.name)
    print()

if failed:
    sys.exit(f"FAILED: {', '.join(failed)}")
print("all test files passed")
