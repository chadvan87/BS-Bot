from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# app.main reads DATA_DIR at import time; keep tests away from the real database and charts.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="signal-studio-tests-"))
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
