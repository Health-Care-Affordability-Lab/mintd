"""Fake `InitOps` for tests — records calls without subprocess."""

from __future__ import annotations

from pathlib import Path

from mintd._init_ops import InitOpError


class _FakeInitOps:
    """Implements `mintd._init_ops.InitOps` structurally.

    Records every call. ``fail_on`` lets tests inject a failure on a
    specific method (e.g. ``{"dvc_remote_add"}``) to exercise the
    rollback path in ``init_project``.
    """

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.git_calls: list[Path] = []
        self.dvc_calls: list[Path] = []
        self.remote_add_calls: list[dict] = []
        self.git_add_calls: list[tuple[Path, list[str]]] = []
        self.git_unstage_calls: list[tuple[Path, list[str]]] = []
        # Ordered log of method names, so tests can assert relative
        # sequencing (e.g. git_add fires after dvc_remote_add) without a
        # per-method timestamp.
        self.call_log: list[str] = []
        self.fail_on: set[str] = fail_on or set()
        # Remotes already in .dvc/config before init runs, so tests can
        # model a repo whose remote of this name is somebody else's.
        self.existing_remotes: dict[str, str] = {}
        # Full per-remote option set, so tests can see that a rerun
        # merges rather than replacing.
        self.remote_configs: dict[str, dict[str, str]] = {}
        self.default_remote: str | None = None
        # The repo's `origin`, RAW as git prints it -- so a test can hand over
        # the scp-like `git@host:org/repo.git` form, a trailing `.git`, a
        # non-GitHub host, or a bare local path, and init's real normalizer
        # runs on it. None means the repo has no origin, which is the plain
        # `mintd init` case (git_init just made the repo).
        self.origin_url: str | None = None

    def git_init(self, target_dir: Path) -> None:
        if "git_init" in self.fail_on:
            raise InitOpError("fake git_init failure")
        self.call_log.append("git_init")
        self.git_calls.append(target_dir)

    def git_add(self, target_dir: Path, paths: list[str]) -> None:
        if "git_add" in self.fail_on:
            raise InitOpError("fake git_add failure")
        self.call_log.append("git_add")
        self.git_add_calls.append((target_dir, list(paths)))

    def git_unstage(self, target_dir: Path, paths: list[str]) -> None:
        # Best-effort in production (never raises); the fake just records.
        self.call_log.append("git_unstage")
        self.git_unstage_calls.append((target_dir, list(paths)))

    def dvc_init(self, target_dir: Path) -> None:
        if "dvc_init" in self.fail_on:
            raise InitOpError("fake dvc_init failure")
        self.call_log.append("dvc_init")
        self.dvc_calls.append(target_dir)

    def dvc_remote_url(self, target_dir: Path, name: str) -> str | None:
        return self.existing_remotes.get(name)

    def git_origin_url(self, target_dir: Path) -> str | None:
        # `fail_on` reaches this one too: the real seam swallows a missing
        # binary and a timeout but not, say, a PermissionError on cwd, and
        # init must survive that -- which is untestable if the double can
        # only ever succeed.
        if "git_origin_url" in self.fail_on:
            raise InitOpError("fake git_origin_url failure")
        self.call_log.append("git_origin_url")
        return self.origin_url

    def dvc_remote_add(
        self,
        target_dir: Path,
        *,
        name: str,
        url: str,
        default: bool,
        endpoint: str | None,
        profile: str | None,
        exists: bool = False,
    ) -> None:
        if "dvc_remote_add" in self.fail_on:
            raise InitOpError("fake dvc_remote_add failure")
        # Mirror real dvc: `remote add` on an existing name fails, and the
        # follow-up `remote modify` calls MERGE into the section rather than
        # replacing it -- which is why `exists` skips only the add.
        if name in self.existing_remotes and not exists:
            raise InitOpError(f"remote {name!r} already exists")
        self.existing_remotes[name] = url
        cfg = self.remote_configs.setdefault(name, {})
        cfg["url"] = url
        if endpoint:
            cfg["endpointurl"] = endpoint
        if profile:
            cfg["profile"] = profile
        cfg["version_aware"] = "true"
        if default:
            self.default_remote = name
        self.call_log.append("dvc_remote_add")
        self.remote_add_calls.append(
            {
                "target_dir": target_dir,
                "name": name,
                "url": url,
                "exists": exists,
                "default": default,
                "endpoint": endpoint,
                "profile": profile,
            }
        )
