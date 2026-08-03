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
    if argv and argv[0] == "--selftest":
        # Used by build.ps1 right after packaging the GUI shell, to verify
        # the frozen exe actually has a working tkinter bundled: importing
        # app.gui exercises the same `import tkinter` a real launch hits,
        # without opening a window or calling mainloop(). See the tkinter
        # checks in build.ps1 for why this needs checking explicitly --
        # PyInstaller can silently omit tkinter instead of failing the build.
        from app import gui  # noqa: F401
        print("selftest ok")
        return 0
    from app import gui
    gui.launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
