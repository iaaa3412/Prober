"""Launch the Atomica Tester GUI (Engineer GUI).

The simplified Operator GUI launches separately: python OperatorGUI/operator_app.py
"""

import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, os.path.join(ROOT, "EngineerGUI")):
    if path not in sys.path:
        sys.path.insert(0, path)

if __name__ == "__main__":
    runpy.run_path(os.path.join(ROOT, "EngineerGUI", "app.py"), run_name="__main__")
