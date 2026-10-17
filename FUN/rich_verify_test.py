"""Compatibility smoke entry point; verifies the new link-preview contract offline."""
import subprocess
import sys
from pathlib import Path
if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable,"-m","pytest","-q","test_link_previews.py"],
        cwd=Path(__file__).resolve().parent))
