"""Launch the Atomica Tester GUI."""

import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, os.path.join(ROOT, "gui")):
    if path not in sys.path:
        sys.path.insert(0, path)

if __name__ == "__main__":
    runpy.run_path(os.path.join(ROOT, "gui", "app.py"), run_name="__main__")
