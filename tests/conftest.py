"""Make `scripts/` importable, so the standalone scanners can be unit tested."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
