"""Start and close the Tkinter application under a virtual display."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with tempfile.TemporaryDirectory(prefix="autowork-gui-smoke-") as data_dir:
    os.environ["LOCALAPPDATA"] = data_dir
    from main import AutoWorkAgent

    root = AutoWorkAgent()
    assert root.winfo_exists() == 1
    root.after(100, root._on_close)
    root.mainloop()
print("GUI_SMOKE_OK")
