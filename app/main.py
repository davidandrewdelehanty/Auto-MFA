"""Auto-MFA entry point.

Supported launch modes (all behave identically):
  * python -m app.main            (running from source as a module)
  * python app/main.py            (running the file directly as a script)
  * Auto-MFA.exe                  (PyInstaller-frozen entry script)

Passing ``--worker <mode> ...`` starts a headless worker process used by the
GUI for running MFA; otherwise the Tkinter GUI is launched.
"""

import multiprocessing
import os
import sys

if __package__ in (None, ""):
    # We were launched as a bare script (python app/main.py), so the parent
    # directory is not on sys.path and the `app` package is not importable yet.
    # Add it so absolute imports below work.  (No-op under `-m` or PyInstaller,
    # where the package is already importable.)
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)


def main() -> int:
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":
        from app import worker
        return worker.main(argv)
    from app import gui
    gui.launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
