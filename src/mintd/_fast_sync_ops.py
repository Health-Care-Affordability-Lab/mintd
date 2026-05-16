"""Forward declaration of the fast-sync optimization seam.

Slice 17 declares the Protocol; slice 18 ships `SubprocessFastSyncOps`
with boto3, S3 reads, and the DVC-version gate. When the Protocol is
absent (i.e., `fast_sync_ops=None`), `data_pull` falls through to
`dvc_ops.pull(...)` unchanged.

The Protocol shape may need to widen in slice 18 — that's the binding
question of slice 17. Keep this module dependency-free; importing it
must not pull in boto3 (slice 18's job).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class FastSyncOps(Protocol):
    """Optional fast-path for `dvc pull`.

    Implementations return True when they succeeded (caller skips dvc pull)
    and False when they couldn't take the fast path (caller falls back).
    Raising is allowed but should be rare; the caller treats it as 'fall
    through' for resilience.
    """

    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str] | None = None,
    ) -> bool: ...
