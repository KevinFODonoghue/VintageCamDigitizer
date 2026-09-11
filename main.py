"""
VintageCam Digitizer — start here:

    python main.py

If Python can't find PyAV or PySide6 you're probably running the system Python
rather than the project's virtual environment.  This script notices that and
re-runs itself with ``.venv\\Scripts\\python.exe``, so ``python main.py`` works
whether or not the venv is activated.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PYTHON = HERE / ".venv" / "Scripts" / "python.exe"


def _relaunch_in_venv_if_needed() -> int | None:
    """Return an exit code if we handled things here, or None to start the app."""
    try:
        import av  # noqa: F401
        import numpy  # noqa: F401
        import PySide6  # noqa: F401

        return None
    except ImportError:
        pass
    if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        return subprocess.call([str(VENV_PYTHON), str(HERE / "main.py"), *sys.argv[1:]])
    sys.stderr.write(
        "VintageCam Digitizer needs PyAV, PySide6, numpy and pyqtgraph.\n"
        "Create the virtual environment first (see README.md):\n\n"
        "    python -m venv .venv\n"
        "    .\\.venv\\Scripts\\python -m pip install -r requirements.txt\n"
    )
    return 1


if __name__ == "__main__":
    code = _relaunch_in_venv_if_needed()
    if code is not None:
        sys.exit(code)
    from vintagecam.app import run

    sys.exit(run())
