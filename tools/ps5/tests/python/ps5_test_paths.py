"""Source and fixture paths shared by PS5 host-side Python tests."""

from pathlib import Path
import sys

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[3]
HERE = ROOT / "tools/ps5"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
