import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(model=None):
    binary = ROOT / "build" / ("exchange.exe" if os.name == "nt" else "exchange")
    if not binary.is_file():
        raise RuntimeError("The packaged exchange executable is missing")
    return binary
