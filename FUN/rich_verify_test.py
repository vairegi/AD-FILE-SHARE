"""Compatibility smoke entry: offline feed-renderer contract tests."""
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", "-q", "test_cards.py"],
        cwd=Path(__file__).resolve().parent))
