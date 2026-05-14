"""Pytest-Conftest fuer DF-LEXVANCE-DATEV-BRIDGE-OPTION-C [CRUX-MK]."""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
