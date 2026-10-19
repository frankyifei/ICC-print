"""Double-click / autostart launcher for the ICC Spooler GUI.

Opens the Toga window; use --minimized (e.g. from autostart) to start hidden in
the system tray.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icc_spooler.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
