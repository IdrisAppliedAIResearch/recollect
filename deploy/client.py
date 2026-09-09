# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#   "fastapi==0.141.1",
#   "uvicorn==0.52.3",
#   "httpx==0.28.1",
#   "python-dotenv==1.2.3",
#   "websockets==17.0.1",
# ]
# ///
"""Run the Surface UI without installing desktop model dependencies."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recollect.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["serve", *sys.argv[1:], "--mode", "client"]))
