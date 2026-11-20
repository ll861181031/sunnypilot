import sys
from pathlib import Path

SHIM_DIR = Path(__file__).resolve().parents[1]
if str(SHIM_DIR) not in sys.path:
    sys.path.insert(0, str(SHIM_DIR))
