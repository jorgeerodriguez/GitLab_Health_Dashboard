#!/usr/bin/env python3
"""Single-command launcher for the Service Accounts Streamlit dashboard.

Run:
    python launch_service_accounts_dashboard.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DASHBOARD_FILE = ROOT / "Service_Accounts_Dashboard.py"


def main() -> int:
    if not DASHBOARD_FILE.exists():
        print(f"Dashboard file not found: {DASHBOARD_FILE}")
        return 1

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(DASHBOARD_FILE),
        "--server.headless",
        "false",
    ]

    return subprocess.call(command, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
