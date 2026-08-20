"""A real git producer the CLI can fetch from.

The capability no helper in the tree has today is **movement**: a producer
whose HEAD advances while `data_products.primary` stays put, and a tag that
can be re-pointed at a later commit. `_view_with_primary`
(`tests/test_check.py:170`) cannot express either — it hands back a fixed
`ProducerView` built around one pin — so every drift/staleness question has
had to be asked of a double's return value instead of an artifact.

`tests/test_producer_integration.py` keeps its own `_init_producer_bare_repo`:
that helper's 2-key metadata stub is the *subject* of its fetcher tests, not a
world they need, so reusing this builder there would change what those tests
assert. Only `_git` is shared.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests._harness.git import _git
from tests._harness.payload import publish_payload, publish_pipeline_payload

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"


@dataclasses.dataclass
class LocalProducer:
    """A bare repo (what consumers fetch from) plus a work clone (what the
    harness commits into). Every mutator pushes, so `url` always serves the
    state the last call left behind."""

    bare: Path
    work: Path
    remote: Path

    @property
    def url(self) -> str:
        return str(self.bare)

    @property
    def head_sha(self) -> str:
        return _git(["rev-parse", "HEAD"], cwd=self.work).strip()

    # -- metadata -----------------------------------------------------------

    def metadata(self) -> dict:
        return json.loads((self.work / "metadata.json").read_text(encoding="utf-8"))

    def _write_metadata(self, meta: dict) -> None:
        (self.work / "metadata.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

    # -- movement -----------------------------------------------------------

    def _commit_and_push(self, message: str) -> str:
        _git(["add", "-A"], cwd=self.work)
        _git(["commit", "-m", message], cwd=self.work)
        _git(["push", "origin", "main"], cwd=self.work)
        return self.head_sha

    def commit_more(self, message: str = "more work") -> str:
        """Advance HEAD **without** touching `data_products`.

        This is the drift case the researcher hits: the producer moved, but
        whether the *product* moved is a separate question.
        """
        notes = self.work / "NOTES.md"
        prior = notes.read_text(encoding="utf-8") if notes.exists() else ""
        notes.write_text(prior + message + "\n", encoding="utf-8")
        return self._commit_and_push(message)

    def rename_primary(self, new_path: str) -> str:
        """Advance HEAD and move the primary product, keeping the primary
        `outputs` entry in step with `data_products.primary`."""
        meta = self.metadata()
        meta["data_products"]["primary"] = new_path
        for out in meta["data_products"].get("outputs") or []:
            if out.get("primary"):
                out["path"] = new_path
        self._write_metadata(meta)
        return self._commit_and_push(f"primary -> {new_path}")

    # -- tags ---------------------------------------------------------------

    def tag(self, name: str, *, rev: str = "HEAD") -> None:
        _git(["tag", "-a", name, "-m", name, rev], cwd=self.work)
        _git(["push", "origin", name], cwd=self.work)

    def move_tag(self, name: str, *, rev: str = "HEAD") -> None:
        _git(["tag", "-f", "-a", name, "-m", name, rev], cwd=self.work)
        _git(["push", "--force", "origin", name], cwd=self.work)

    def local_tags(self) -> list[str]:
        return _git(["tag", "--list"], cwd=self.work).split()

    def remote_tags(self) -> list[str]:
        """Tag names as the *remote* reports them — not the work clone's refs,
        which can lag a force-push."""
        out = _git(["ls-remote", "--tags", self.url])
        return sorted(
            {
                line.split("refs/tags/", 1)[1].removesuffix("^{}")
                for line in out.splitlines()
                if "refs/tags/" in line
            }
        )

    def resolve_remote_tag(self, name: str) -> str:
        """Ask the remote, from scratch, what commit `name` points at.

        Peels the annotated tag (`^{}`) so the answer is a commit SHA and not
        the tag object's own SHA — which is what moves when a tag is re-made.
        """
        out = _git(["ls-remote", self.url, f"refs/tags/{name}^{{}}"])
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert lines, f"no peeled ref for tag {name!r} on {self.url}"
        return lines[0].split()[0]

    # -- payload -------------------------------------------------------------

    def publish(
        self,
        outs: Mapping[str, bytes | Mapping[str, bytes]],
        *,
        message: str = "payload",
    ) -> str:
        """DVC-track `outs`, push the blobs, and land the pointers in a commit.

        Two halves, and the second is not optional. `publish_payload` pushes
        blobs to `remote` and writes `.dvc/` + the pointer files into the work
        tree, but commits nothing — a consumer cloning at that point gets no
        `.dvc/` and `dvc pull` exits 253 "not inside of a DVC repository".
        Committing here rather than inside `publish_payload` keeps the branch
        the producer's business and the bytes the payload's.

        **This advances HEAD**, so it is a peer of `commit_more()`, not a
        prelude to one: a caller wanting "the payload landed at commit N+1"
        calls `publish()` alone. `publish()` then `commit_more()` puts the
        payload at N+1 and leaves HEAD at N+2, which is a different world and
        almost never the one the test meant.
        """
        publish_payload(self.work, self.remote, outs)
        return self._commit_and_push(message)

    def publish_pipeline(
        self,
        dvc_yaml: str,
        *,
        seed: Mapping[str, bytes | Mapping[str, bytes]] | None = None,
        message: str = "pipeline payload",
    ) -> str:
        """`publish()`'s pipeline twin: a real stage run, so `dvc.lock` is
        dvc's own output rather than authored text. Same commit semantics."""
        publish_pipeline_payload(self.work, self.remote, dvc_yaml, seed=seed)
        return self._commit_and_push(message)


def build_local_producer(root: Path) -> LocalProducer:
    """Bare repo + work clone, seeded with full v2 metadata on `main`."""
    bare = root / "producer.git"
    work = root / "producer"
    remote = root / "dvc-remote"
    _git(["init", "--bare", "-b", "main", str(bare)])
    _git(["clone", str(bare), str(work)])
    _git(["checkout", "-b", "main"], cwd=work)

    shutil.copy(V2_MINIMAL, work / "metadata.json")
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-m", "seed"], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)

    # `git archive --remote` is GitArchiveFetcher's fast path. The fetcher
    # always sends a SHA (`_git_ls_remote_head` resolves HEAD first), and a
    # SHA is by definition not an advertised ref, so the remote refuses it
    # unless this is set — verified at git 2.48.1: rc=1 `no such ref` without,
    # rc=0 with. `tests/test_producer_integration.py:52` writes
    # `uploadarch.allowed`, which is not a git key at all (the real ones are
    # `uploadarchive.allowUnreachable` here and `daemon.uploadarch` in
    # git-daemon); git accepts the unknown key silently, so that fixture has
    # only ever exercised `_fallback_clone`. See the close-out follow-up.
    _git([
        "config", "-f", str(bare / "config"),
        "uploadarchive.allowUnreachable", "true",
    ])
    return LocalProducer(bare=bare, work=work, remote=remote)


@pytest.fixture
def local_producer(tmp_path: Path) -> LocalProducer:
    return build_local_producer(tmp_path / "prod")
