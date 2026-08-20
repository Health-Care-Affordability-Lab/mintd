"""Real DVC-tracked bytes, published in-process.

The suite has never had a producer that serves *data*. Every "the consumer
pulled it" assertion until now has been a double's return value, because
building real payload meant spawning `dvc` per output — measured at **1.95s**
marginal per producer at `b7b1102`, against **0.06s** for the same work through
`dvc.repo.Repo` in-process. That 26x is the whole reason this module exists;
see Approach C in `notes/mintd-check/PLAN-hermetic-harness.md`.

Choosing the in-process API over the CLI buys speed and owes fidelity: mintd
pins `dvc >= 3.66, < 4.0` (`pyproject.toml:12`), a **range**, so `Repo`'s
behaviour is free to drift from the `dvc` argv mintd itself runs. That debt is
paid by `test_published_payload_is_byte_identical_to_a_subprocess_dvc_push`,
which builds one payload both ways and diffs the artifacts. If that test ever
reds, this module is wrong and the CLI is right.

**Nothing here commits.** `publish_payload` writes `.dvc/config`, the pointer
files and the payload into the work tree and pushes blobs to the remote — a
consumer cloning at that moment gets no `.dvc/` at all and `dvc pull` exits 253
with "you are not inside of a DVC repository". Committing is
`LocalProducer.publish()`'s half of the seam, deliberately, because the branch
belongs to the producer and not to the payload.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from pathlib import Path


def _configure_remote(repo, remote: Path) -> None:
    remote.mkdir(parents=True, exist_ok=True)
    with repo.config.edit() as conf:
        conf["remote"]["storage"] = {"url": str(remote)}
        conf["core"]["remote"] = "storage"


def _write_out(work: Path, rel: str, body: bytes | Mapping[str, bytes]) -> Path:
    """Materialize one out. A mapping means a directory out — the shape that
    exercises dvc's `.dir` manifest, which is where a hand-authored cache
    (mechanism (b)) would have had to re-implement dvc's own serialization."""
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, Mapping):
        p.mkdir(exist_ok=True)
        for name, blob in body.items():
            (p / name).write_bytes(blob)
    else:
        p.write_bytes(body)
    return p


def publish_payload(
    work: Path,
    remote: Path,
    outs: Mapping[str, bytes | Mapping[str, bytes]],
) -> None:
    """`dvc add` each out and push to a local-directory remote.

    PRECONDITION: `work` is already a git worktree. Against a plain directory
    `Repo.init` raises `InitError("… not tracked by any supported SCM tool")`.

    Leaves the tree dirty on purpose — see the module docstring.
    """
    from dvc.repo import Repo

    # `Repo.init` raises `InitError("'.dvc' exists. Use `-f` to force.")` on a
    # tree that already has one, so a producer can only ever publish ONCE
    # unless this branches. Publishing twice is not an edge case — it is the
    # drift journey (v1 bytes, then v2 bytes at a later commit), which is the
    # whole reason `local_producer` can move.
    repo = Repo(str(work)) if (work / ".dvc").is_dir() else Repo.init(str(work))
    try:
        _configure_remote(repo, remote)
        for rel, body in outs.items():
            # ABSOLUTE path: `Repo` resolves a relative arg against the
            # *process* cwd, not against its own root, so a relative one here
            # silently adds the wrong file when pytest runs from the repo root.
            repo.add(str(_write_out(work, rel, body)))
        repo.push()
    finally:
        repo.close()


def publish_pipeline_payload(
    work: Path,
    remote: Path,
    dvc_yaml: str,
    *,
    seed: Mapping[str, bytes | Mapping[str, bytes]] | None = None,
) -> None:
    """Author a `dvc.yaml`, run the stage for real, push what it produced.

    This is the only way in the tree to get a `dvc.lock` that dvc itself
    wrote — every other lock is hand-authored text, so `parse_dvc_lock_outs`
    has never been fed dvc's own output.

    **The chdir is load-bearing. Re-measured at `33f01e6`, dvc 3.67.1, in a
    minimal harness — `Repo.init`, write `dvc.yaml`, then inside
    `contextlib.chdir(work)`:**

        A  reuse the `Repo.init(str(work))` object : NetworkXError
        B  fresh `Repo(str(work))`  (absolute)     : NetworkXError
        C  fresh `Repo(".")`                       : OK, dvc.lock written

    Both failures are `The node stage: 'build' is not in the digraph` — the
    stage is read from `dvc.yaml` under one root and looked up under another.

    **But one half of that is not load-bearing HERE, and the plan states it too
    strongly.** Mutation-tested through this function at ship:

      - remove the `chdir` → `test_pipeline_stage_out_is_servable` REDDENS.
      - swap `Repo(".")` for `Repo(str(work))` → still GREEN, even with the
        init repo deliberately left open.

    Closing the init repo before reopening (and `_configure_remote`'s
    `config.edit()` in between) is apparently enough to rebuild the index that
    A and B trip over. So `Repo(".")` is kept because it is the one form that
    worked under every condition measured, minimal harness included — but it is
    DEFENSIVE here, not pinned by a test, and calling it "as load-bearing as
    the chdir" would be a claim this file cannot back. The chdir is the part
    with teeth.
    """
    from dvc.repo import Repo

    for rel, body in (seed or {}).items():
        _write_out(work, rel, body)
    (work / "dvc.yaml").write_text(dvc_yaml, encoding="utf-8")

    # `Repo(".")` OPENS an existing repo; it does not create one. The plan's
    # transcript ran this form on a tree `publish_payload` had already
    # initialized, so the init read as incidental — standalone it raises
    # `NotDvcRepoError`. Init here (absolute, outside the chdir, where it is
    # correct) so the pipeline variant does not depend on call order.
    if not (work / ".dvc").is_dir():
        Repo.init(str(work)).close()

    with contextlib.chdir(work):
        repo = Repo(".")
        try:
            _configure_remote(repo, remote)
            repo.reproduce()
            repo.push()
        finally:
            repo.close()
