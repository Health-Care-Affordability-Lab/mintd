"""Helper to invoke mintd's bundled dvc."""

import sys

def dvc_cmd() -> list[str]:
    """Return the subprocess argv prefix for invoking mintd's bundled dvc.
    Uses ``sys.executable -m dvc`` so the dvc that runs is the one
    installed in mintd's own Python env (per pyproject), not whatever
    happens to be first on PATH. See SLICE-40."""
    return [sys.executable, "-m", "dvc"]
