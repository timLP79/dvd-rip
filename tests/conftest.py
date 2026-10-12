import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Pin the optical device for the whole suite. Without this, hostenv falls back to
# live detection, which shells out to `drutil` on macOS — tests would depend on
# whether a drive happens to be plugged into the machine running them. Setting the
# documented override exercises the same code path on every host.
os.environ.setdefault("DVD_DEVICE", "/dev/sr0")
os.environ.setdefault("DISCID_DEVICE", "/dev/sr0")
