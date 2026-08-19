"""Real `dvc`, run hermetically.

`dvc_cmd()` is imported from production (`src/mintd/_dvc_invoke.py`) rather
than re-declared: the argv that runs here must be the argv mintd runs, or a
contract test proves something about a different dvc.

dvc's config and cache locations are redirected under `tmp_path` so a run never
reads the developer's global or system dvc config and never shares the
machine's site cache with a parallel cell.

The knobs are dvc's own — `DVC_GLOBAL_CONFIG_DIR`, `DVC_SYSTEM_CONFIG_DIR`,
`DVC_SITE_CACHE_DIR` (`dvc/dirs.py`) — not `HOME`. Redirecting `HOME` only
works on the platforms where `platformdirs` happens to consult it: on Windows
`user_config_dir` resolves through `%LOCALAPPDATA%` and never reads `HOME` at
all, and on Linux `XDG_CONFIG_HOME` takes precedence over it. Either way the
redirect fails **open** — the run silently reads the real config, which is the
one thing this fixture exists to prevent. `HOME`/`USERPROFILE` are still moved,
for everything else a subprocess might drop in a home directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mintd._dvc_invoke import dvc_cmd, dvc_env


@pytest.fixture
def real_dvc(tmp_path: Path):
    home = tmp_path / "dvc-home"
    site = tmp_path / "dvc-site"
    home.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)

    # `dvc_env()` is production's — it already carries DVC_NO_ANALYTICS, and a
    # hand-rolled copy here could drift from what mintd actually spawns.
    env = {
        **dvc_env(),
        "HOME": str(home),
        "USERPROFILE": str(home),  # HOME's Windows equivalent
        "DVC_GLOBAL_CONFIG_DIR": str(home / "dvc-global"),
        "DVC_SYSTEM_CONFIG_DIR": str(home / "dvc-system"),
        "DVC_SITE_CACHE_DIR": str(site),
    }

    def run(
        args: list[str], *, cwd: Path, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*dvc_cmd(), *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    # Exposed so the redirect is assertable without a dvc call that WRITES —
    # a probe via `dvc config --global` lands in the developer's real
    # `~/Library/Application Support/dvc/config` on exactly the run where the
    # redirect is broken, which is the one run that must not touch their
    # machine.
    run.env = env
    return run
