"""The only module in mintd that shells out to `gh`; `git` also runs in
`_init_ops.py` and `_producer_git_ops.py`.

Every other module that needs to interact with the registry repo (the cache,
GitCatalogClient) goes through the `RegistryGitOps` Protocol. Production code
uses `SubprocessRegistryGitOps`; tests inject a fake.

This seam:
  - Normalizes subprocess failures into typed exceptions.
  - Lets us swap implementations without touching callers.
  - Bounds the surface area of code that depends on `git` / `gh` being on PATH.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass

from ._git_invoke import git_env
from .errors import MintdError
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from urllib.parse import urlsplit

import yaml


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


#: The files a version-aware push rewrites: the pointers. ``top`` anchors them
#: at the repo root, because `dvc push` uploads the whole repo from any
#: subdirectory.
_POINTER_PATHSPEC = (":(top,glob)**/*.dvc", ":(top,glob)**/dvc.lock")


def is_pointer_path(path: str) -> bool:
    """Does ``path`` (repo-relative, posix) match ``_POINTER_PATHSPEC``? Keep
    the two in step."""
    return path.endswith(".dvc") or path == "dvc.lock" or path.endswith("/dvc.lock")


@dataclass(frozen=True)
class BranchInfo:
    """The checked-out branch and its upstream. ``branch`` is None when HEAD is
    detached; ``remote`` / ``merge_ref`` are None with no upstream. ``ahead``
    counts the commits not on the upstream, or, with no upstream or one of
    another name, the commits on no remote at all; None when git cannot
    count them."""

    branch: str | None
    remote: str | None
    merge_ref: str | None
    ahead: int | None


#: The hint for a git failure where no repository URL is known; with one,
#: ``github_access_hint`` says more.
GITHUB_ACCESS_HINT = "check GitHub access with `gh auth status`; git uses gh after `gh auth setup-git`"


class GitOpError(MintdError):
    """A `git` invocation failed (non-zero exit). Carries stderr for diagnostics."""

    hint = GITHUB_ACCESS_HINT

    def __init__(self, command: list[str], stderr: str, hint: str | None = None) -> None:
        super().__init__(f"git command failed: {' '.join(command)}\n{stderr}", hint=hint)
        self.command = command
        self.stderr = stderr


# ---------------------------------------------------------------------------
# GitHub access: through gh, the one supported way
# ---------------------------------------------------------------------------

#: What turns both SSH forms into https, so git reaches GitHub through gh.
SSH_TO_HTTPS = (
    'git config --global --add url."https://github.com/".insteadOf git@github.com: ; '
    'git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/'
)


#: Said after the GitHub access hint where git failed to reach the repo: the
#: same failure can be the network.
NETWORK_TOO = "if GitHub access is fine, check your network"


class GitHubAccessMissing(GitOpError):
    """mintd reaches GitHub through gh only, and gh isn't ready for this
    repository. Raised before git runs, as a ``GitOpError``, so every caller
    of the git seam handles it as it handles git failing."""

    def __init__(self, url: str, hint: str) -> None:
        reason = f"mintd reaches GitHub through gh, and it isn't ready for {url}"
        MintdError.__init__(self, reason, hint=hint)
        self.command: list[str] = []
        self.stderr = reason


class RepoURL(NamedTuple):
    """A git remote URL, read the way git reads it."""

    transport: str  # "https", "http", "ssh" or "git"
    host: str  # case-folded, without the port
    owner: str
    repo: str  # without ".git"; may hold "/" on a host with nested groups
    userinfo: str  # kept apart: never part of a URL mintd prints or compares
    port: str


#: The URL schemes git takes for a remote, by the transport each one uses.
_SCHEMES = {"https": "https", "http": "http", "git": "git", "ssh": "ssh", "git+ssh": "ssh", "ssh+git": "ssh"}
#: GitHub's hosts: the web host, its www alias, its SSH-over-443 host and gists.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com", "gist.github.com"})
_GIST = "gist.github.com"


def parse_repo_url(url: str) -> RepoURL | None:
    """``url`` in every form git accepts for a remote -- ``scheme://`` (https,
    http, git, ssh, git+ssh), or scp-like ``[user@]host:path`` -- or None for a
    local path, ``file://`` or anything without an owner and a repo. The one
    parser behind ``github_owner``, ``https_remote`` and the SSH check, so the
    three can't disagree."""
    url = url.strip()
    if "://" in url:
        scheme, _, rest = url.partition("://")
        transport = _SCHEMES.get(scheme.lower())
        authority, _, path = rest.partition("/")
    else:
        # git's own rule: scp-like when a colon comes before any slash; one
        # letter before it is a Windows drive, a local path.
        authority, colon, path = url.partition(":")
        transport = "ssh" if colon and "/" not in authority and len(authority) > 1 else None
    if transport is None:
        return None
    userinfo, _, hostport = authority.rpartition("@")
    host, _, port = hostport.partition(":")
    host = host.lower().rstrip(".")  # "github.com." is github.com, fully qualified
    parts = [p for p in path.split("/") if p]
    if not host or len(parts) < (1 if host == _GIST else 2):  # a gist is one part: its id
        return None
    owner, repo = (parts[0], "/".join(parts[1:])) if len(parts) > 1 else ("", parts[0])
    return RepoURL(transport, host, owner, repo.removesuffix(".git"), userinfo, port)


def _on_github(repo: RepoURL | None) -> bool:
    return repo is not None and repo.host in _GITHUB_HOSTS


def _credential_host(repo: RepoURL) -> str:
    """The host git sends credentials to for a GitHub repository: a gist's
    own host, else github.com (GitHub answers www.github.com with a redirect
    there, and git then asks github.com's credential helper)."""
    return _GIST if repo.host == _GIST else "github.com"


def _credentials_out(url: str) -> str:
    """``url`` with its credentials taken out of its structure and its shape
    kept: for http(s) all of the userinfo, for another scheme the password (a
    login name is no secret, and SSH needs it), and scp-like
    ``user:pw@host:path`` becomes ``user@host:path``."""
    url = url.strip()
    if "://" in url:
        # The userinfo ends at the last "@" before the first "/" after the
        # scheme, as git reads it. Not urlsplit's netloc, which also stops at
        # a "#" or "?" a password may hold.
        scheme, _, rest = url.partition("://")
        authority = rest.split("/", 1)[0]
        userinfo, at, hostport = authority.rpartition("@")
        if not at:
            return url
        http = scheme.lower() in ("http", "https", "git+http", "git+https")
        login = "" if http else f"{userinfo.split(':', 1)[0]}@"
        return f"{scheme}://{login}{hostport}{rest[len(authority):]}"
    userinfo, at, rest = url.rpartition("@")
    if at and ":" in rest and "/" not in userinfo:  # scp-like with userinfo; not an "@" in a path
        return f"{userinfo.split(':', 1)[0]}@{rest}"
    return url


def https_remote(raw: str) -> str:
    """A git remote URL in its https form, the one repo identity mintd compares
    and the URL dvc records: ``git@GitHub.com:org/repo.git``,
    ``ssh://git@ssh.github.com:443/org/repo`` and ``https://u@github.com/org/repo/``
    all give ``https://github.com/org/repo``. Credentials never come along. A
    URL with no owner and repo, or a local path, keeps its shape, less a
    trailing ``.git``."""
    raw = _credentials_out(raw)
    repo = parse_repo_url(raw)
    if repo is None:
        return raw.removesuffix(".git")
    if _on_github(repo):
        host = _credential_host(repo)
    else:  # another host keeps an https port; an ssh port means nothing to https
        keep_port = repo.port and repo.transport in ("https", "http")
        host = f"{repo.host}:{repo.port}" if keep_port else repo.host
    return f"https://{host}/{'/'.join(filter(None, (repo.owner, repo.repo)))}"


def without_credentials(url: str) -> str:
    """The URL dvc is given, and writes into a committed .dvc file: GitHub's
    https form, or any other host's URL in its own shape with the credentials
    taken out (``_credentials_out``). Never rebuilt from owner and repo, which
    would lose a one-part path or turn an scp-like path relative to the
    login's home into an absolute one. ``https_remote`` of this and of the
    catalog URL agree, so a re-import or bump knows its own producer."""
    url = _credentials_out(url)
    return https_remote(url) if _on_github(parse_repo_url(url)) else url


def github_owner(url: str) -> str | None:
    """The owner (org or user) of a GitHub repository URL, in any form, or
    None for anything else: a local path, another host."""
    repo = parse_repo_url(url)
    return (repo.owner or repo.host) if _on_github(repo) and repo is not None else None


def _git_output(argv: list[str], cwd: Path | None) -> list[str]:
    """The non-blank lines a local git command prints. Raises OSError or
    TimeoutExpired when git can't answer."""
    out = subprocess.run(
        argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=10, env=git_env(), check=False,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def urls_git_uses(url: str, *, cwd: Path | None = None, push: bool = False) -> list[str]:
    """Every URL git will actually reach for ``url`` (a URL or a remote name):
    with ``insteadOf`` applied, and for a push every ``pushurl`` with
    ``pushInsteadOf`` too. Local: none of these commands talks to a remote.
    When git can't say, this refuses rather than guess (GitHubAccessMissing)."""
    try:
        # A remote's push URLs, if ``url`` names one in the repository.
        urls = _git_output(["git", "remote", "get-url", "--push", "--all", url], cwd) if push and cwd else []
        if not urls and push:  # a URL: git's push rule, which `remote get-url` won't apply to it
            urls = [rewritten] if (rewritten := _push_insteadof(url, cwd)) else []
        if not urls:
            urls = _git_output(["git", "ls-remote", "--get-url", url], cwd)
    except FileNotFoundError as e:
        raise GitHubAccessMissing(_credentials_out(url), hint="git isn't installed, and mintd runs git: install it") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        # No exception text: a timeout's repeats git's argv, URL and all.
        late = "didn't say in time" if isinstance(e, subprocess.TimeoutExpired) else "couldn't say"
        raise GitHubAccessMissing(
            _credentials_out(url), hint=f"git {late} which URL it uses for this: check that `git remote -v` works",
        ) from e
    return urls or [url]


def _push_insteadof(url: str, cwd: Path | None) -> str | None:
    """``url`` with git's ``url.<base>.pushInsteadOf`` rule applied (the
    longest matching prefix wins), or None when none matches."""
    entries = _git_output(["git", "config", "--get-regexp", r"^url\..*\.pushinsteadof$"], cwd)
    best_base, best_prefix = "", ""
    for entry in entries:
        key, _, prefix = entry.partition(" ")
        if prefix and url.startswith(prefix) and len(prefix) > len(best_prefix):
            best_base, best_prefix = key[len("url."):-len(".pushinsteadof")], prefix
    return best_base + url[len(best_prefix):] if best_prefix else None


def url_git_uses(url: str, *, cwd: Path | None = None, push: bool = False) -> str:
    """The first URL ``urls_git_uses`` gives, or ``url`` itself when git can't
    say: for hints, which must never fail."""
    try:
        return urls_git_uses(url, cwd=cwd, push=push)[0]
    except GitHubAccessMissing:
        return url


def _gh_login() -> str:
    """Whether gh has a token for github.com: "ready", "logged out", "too old"
    (no ``gh auth token``, before gh 2.17), "unreadable" (gh didn't answer in
    time) or "missing". ``gh auth token`` is offline; the token is never kept."""
    try:
        token = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=15, env=git_env(), check=False,
        )
    except FileNotFoundError:
        return "missing"
    except subprocess.TimeoutExpired:
        return "unreadable"
    if token.returncode == 0 and token.stdout.strip():
        return "ready"
    # gh before 2.17 has no `auth token`: given `--hostname`, it says "unknown flag".
    old = "unknown flag" in token.stderr or "unknown command" in token.stderr
    return "too old" if old else "logged out"


def _gh_accounts() -> tuple[str | None, tuple[str, ...]]:
    """The active gh account for github.com and the others, read from gh's
    hosts.yml, for the hint only. (None, ()) when it can't be read, or when
    GH_TOKEN or GITHUB_TOKEN picks the account instead."""
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return None, ()
    if os.environ.get("GH_CONFIG_DIR"):
        config = Path(os.environ["GH_CONFIG_DIR"])
    elif os.environ.get("XDG_CONFIG_HOME"):
        config = Path(os.environ["XDG_CONFIG_HOME"], "gh")
    elif os.name == "nt" and os.environ.get("APPDATA"):
        config = Path(os.environ["APPDATA"], "GitHub CLI")
    else:
        config = Path.home() / ".config" / "gh"
    try:
        host = yaml.safe_load((config / "hosts.yml").read_text(encoding="utf-8"))["github.com"]
        active, users = host.get("user"), host.get("users") or {}
    except (OSError, ValueError, yaml.YAMLError, KeyError, TypeError, AttributeError):
        return None, ()
    if not isinstance(active, str):
        return None, ()
    return active, tuple(str(u) for u in users if u != active)


@functools.lru_cache(maxsize=None)
def _gh_state() -> tuple[str, str | None, tuple[str, ...]]:
    """(gh's login, as ``_gh_login``; the active and the other accounts, for
    the hint only) for github.com, read once per process, never over the network."""
    return _gh_login(), *_gh_accounts()


def _scope_covers(scope: str, host: str) -> bool:
    """Whether a ``credential.<url>.helper`` scope applies to ``https://<host>``:
    git matches the host exactly, so github.com's scope doesn't cover a gist."""
    parts = urlsplit(scope)
    return parts.scheme == "https" and (parts.hostname or "").rstrip(".") == host and parts.path in ("", "/")


def _git_uses_gh(cwd: Path | None, host: str = "github.com") -> bool:
    """Whether gh's helper is among the credential helpers git runs for
    ``https://<host>``: every ``credential.helper`` that applies, in git's
    order, an empty value clearing the list, read with the repository's own
    config. (`gh auth setup-git` writes one for github.com and one for
    gist.github.com.)"""
    try:
        out = subprocess.run(
            ["git", "config", "-z", "--get-regexp", r"^credential\..*helper$"],
            cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=10, env=git_env(), check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    helpers: list[str] = []
    for entry in filter(None, out.split("\0")):
        key, _, value = entry.partition("\n")
        scope = key[len("credential."):-len(".helper")]
        if scope and not _scope_covers(scope, host):
            continue
        helpers = [*helpers, value] if value else []
    return any(_is_gh_helper(helper) for helper in helpers)


def _is_gh_helper(helper: str) -> bool:
    """Whether a ``credential.helper`` value runs gh's helper, read as git runs
    it: a ``!`` value goes through ``sh``, so its words split as ``sh`` splits
    them. The first word is gh at any path (``gh`` or ``gh.exe``, any case),
    and the next two are ``auth git-credential``. That covers what
    `gh auth setup-git` writes everywhere, a quoted path included:
    ``!'C:\\Program Files\\GitHub CLI\\gh.exe' auth git-credential`` on
    Windows, or a POSIX path with a space."""
    if not helper.startswith("!"):
        return False
    try:
        words = shlex.split(helper[1:], posix=True)
    except ValueError:  # unbalanced quotes: sh can't run it either
        return False
    if len(words) != 3:  # gh writes exactly three; a word after them is another command sh would run
        return False
    name = re.split(r"[\\/]", words[0])[-1].lower()
    return name in ("gh", "gh.exe") and words[1:] == ["auth", "git-credential"]


def github_access_hint(url: str, *, cwd: Path | None = None) -> str:
    """The one hint for GitHub access: names the repo's org, the active gh
    account when gh's config says it, and the account to switch to. Nothing
    is recorded, and nothing switches accounts by itself."""
    repo = parse_repo_url(url)
    owner = (repo.owner or repo.host) if repo is not None else "its owner's org"
    https = https_remote(url) if repo is not None else "https://github.com/<org>/<repo>"
    if repo is not None and repo.transport == "ssh":
        return (
            f"mintd reaches GitHub through gh only, and git would use SSH for this repo: run {SSH_TO_HTTPS}; "
            "if git still uses SSH, a url.<base>.insteadOf or pushInsteadOf rule or the remote's own URL "
            f"points it there: change it to {https}"
        )
    if repo is not None and repo.transport != "https":
        return f"mintd reaches GitHub through gh, over https only: use {https}"
    login, active, others = _gh_state()
    if login == "missing":
        return "install gh (https://cli.github.com), then run `gh auth login`, then `gh auth setup-git`"
    if login == "too old":
        return "upgrade gh: mintd needs gh 2.17 or newer (`gh --version`), then run `gh auth setup-git`"
    if login == "unreadable":
        return "gh didn't answer `gh auth token` in time (a locked keychain?): check `gh auth status`, then retry"
    if login == "logged out":
        return "log in to GitHub with gh: run `gh auth login`, then `gh auth setup-git`"
    if not _git_uses_gh(cwd, _credential_host(repo) if repo is not None else "github.com"):
        return "git doesn't use gh yet: run `gh auth setup-git`"
    if active is None:
        return f"this repo is in {owner}; check that your active gh account can see it: `gh auth status`, then `gh auth switch`"
    if len(others) == 1:
        switch = f"run `gh auth switch -u {others[0]}`"
    elif others:
        switch = f"switch to one of {', '.join(others)} with `gh auth switch -u <account>`"
    else:
        switch = f"log in with an account in {owner}: `gh auth login`"
    return f"this repo is in {owner}; your active gh account is {active}; {switch}"


def github_refusal(url: str, *, cwd: Path | None = None, push: bool = False) -> GitHubAccessMissing | None:
    """The refusal git reaching ``url`` would meet, or None when it may go
    ahead: every URL git would use (to push, with ``push``) that is on GitHub
    must be https, with gh holding a token and git using gh. A local path or
    another host passes. The account's name never decides."""
    try:
        used_urls = urls_git_uses(url, cwd=cwd, push=push)
    except GitHubAccessMissing as refused:
        return refused
    for used in used_urls:
        repo = parse_repo_url(used)
        if not _on_github(repo) or repo is None:
            continue
        if repo.transport == "https" and _gh_state()[0] == "ready" and _git_uses_gh(cwd, _credential_host(repo)):
            continue
        return GitHubAccessMissing(_credentials_out(used), hint=github_access_hint(used, cwd=cwd))
    return None


@functools.lru_cache(maxsize=None)
def producer_refusal(url: str) -> GitHubAccessMissing | None:
    """``github_refusal`` for a producer's URL (no repository, a fetch), once
    per process: dvc's seam asks for every import on every pull, which is one
    call per target."""
    return github_refusal(url)


def require_github_access(url: str, *, cwd: Path | None = None, push: bool = False) -> None:
    """Before git reaches ``url``: raise ``github_refusal``'s refusal, if any."""
    refused = github_refusal(url, cwd=cwd, push=push)
    if refused is not None:
        raise refused


#: git subcommands that reach a remote. Every other one stays local, except
#: the ``remote``, ``submodule`` and ``archive`` forms ``_network_target`` names.
_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
#: git's own options that take their value as the next word, before the subcommand.
_GLOBAL_WITH_VALUE = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env"})
#: Options of the network subcommands mintd uses that take no value.
_NO_VALUE = frozenset({
    "--atomic", "--follow-tags", "-u", "--set-upstream", "--heads", "--tags", "--symref", "-q", "--quiet",
    "--prune", "--force", "-f", "--no-tags", "--refs", "--progress", "--all", "--rebase", "--ff-only",
})


def _network_target(args: list[str]) -> tuple[str, str | None] | None:
    """(subcommand, the remote it reaches) for a git argv, or None for a local
    one. The remote is None when it can't be told for certain -- no remote
    named, or an option before it that may take a value -- and the caller then
    checks every remote: fail closed."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GLOBAL_WITH_VALUE else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if sub == "archive":  # local unless --remote names a repository
        for j, arg in enumerate(rest):
            if arg.startswith("--remote="):
                return sub, arg.split("=", 1)[1]
            if arg == "--remote":
                return sub, rest[j + 1] if j + 1 < len(rest) else None
        return None
    if sub == "remote":
        return (sub, None) if rest[:1] in (["update"], ["show"], ["prune"]) else None
    if sub == "submodule":
        return (sub, None) if "update" in rest else None
    if sub not in _NETWORK_SUBCOMMANDS:
        return None
    for arg in rest:
        if not arg.startswith("-"):
            return sub, arg
        if "=" not in arg and arg not in _NO_VALUE:
            return sub, None
    return sub, None


def _every_remote(args: list[str], cwd: Path | None) -> list[str]:
    """When ``_network_target`` can't tell which remote a call reaches: every
    remote the repository has, and every word of the call that is a
    repository URL. Refuses when git can't list them."""
    try:
        remotes = _git_output(["git", "remote"], cwd) if cwd is not None else []
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubAccessMissing(str(cwd), hint=f"git couldn't list this repository's remotes ({e})") from e
    return remotes + [arg for arg in args if parse_repo_url(arg) is not None]


def specific_hint(exc: BaseException) -> str | None:
    """The hint this error carries itself -- a GitHub refusal's, or the account
    hint ``_git`` attaches when GitHub turns git away -- not its class's
    default. A caller with advice of its own keeps this one first."""
    hint = vars(exc).get("hint")
    return hint if isinstance(hint, str) else None


def _unreachable_hint(target: str, stderr: str, *, cwd: Path | None, push: bool = False) -> str | None:
    """github_access_hint for a GitHub remote git couldn't reach, else None
    (the class hint stands)."""
    from ._producer_git_ops import FetchError, _classify_stderr

    if _classify_stderr(stderr) is not FetchError.Reason.UNREACHABLE:
        return None
    used = url_git_uses(target, cwd=cwd, push=push)
    return f"{github_access_hint(used, cwd=cwd)}; {NETWORK_TOO}" if github_owner(used) else None


class GitPushRejected(GitOpError):
    """`git push` was rejected because the remote branch has commits this
    clone does not (non-fast-forward)."""


class GitTagError(GitOpError):
    """`git tag` failed."""


class GitTagAlreadyExists(GitTagError):
    """The tag already exists."""

    def __init__(self, name: str, work_dir: str) -> None:
        super().__init__(["git", "tag", name], f"tag {name} already exists")
        self.name = name
        self.work_dir = work_dir


class GhAuthError(Exception):
    """`gh` reports the user is not authenticated. Caller should prompt
    `gh auth login` and retry."""


class GhNotInstalled(Exception):
    """The `gh` binary is not on PATH."""


class PRConflictError(Exception):
    """`gh pr create` reports an existing PR on the same branch."""

    def __init__(self, branch: str, existing_pr: int | None = None) -> None:
        super().__init__(
            f"PR already exists for branch {branch!r}"
            + (f" (#{existing_pr})" if existing_pr else "")
        )
        self.branch = branch
        self.existing_pr = existing_pr


class RegistryBranchExists(GitOpError):
    """`git push` was rejected: the remote branch has commits we do not have
    (an earlier catalog PR is still open on it)."""

    def __init__(self, command: list[str], stderr: str, branch: str) -> None:
        super().__init__(command, stderr)
        self.branch = branch


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RegistryGitOps(Protocol):
    """Every `gh` subprocess in mintd, and every `git` one outside
    `_init_ops.py` and `_producer_git_ops.py`, goes through one of these
    methods.

    Implementations must:
      - Raise `GitOpError` / `GhAuthError` / `GhNotInstalled` / `PRConflictError`
        on the expected failure modes — no bare `CalledProcessError` leaks.
      - Be re-entrant (clone-then-fetch-then-reset is a normal sequence).
      - Not maintain hidden state outside the filesystem under `repo_dir`.
    """

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None: ...
    def fetch(self, repo_dir: Path) -> None: ...
    def reset_hard(self, repo_dir: Path, ref: str) -> None: ...
    def checkout(self, repo_dir: Path, ref: str, *, force: bool = False) -> None: ...
    def checkout_new_branch(self, repo_dir: Path, branch: str) -> None: ...
    def remote_branch_exists(self, repo_dir: Path, branch: str) -> bool: ...
    def checkout_remote_branch(self, repo_dir: Path, branch: str) -> None: ...
    def index_entries(self, work_dir: Path, names: list[str]) -> dict[str, str]: ...
    def ignored_files(self, work_dir: Path, paths: list[str]) -> list[str]: ...
    def listed_files(self, work_dir: Path, paths: list[str]) -> list[str]: ...
    def restore_index(self, work_dir: Path, names: list[str], entries: dict[str, str]) -> None: ...
    def commit_all(self, repo_dir: Path, message: str) -> None: ...
    def push_branch(self, repo_dir: Path, branch: str) -> None: ...
    def push_with_tags(self, work_dir: Path) -> None: ...
    def tag(self, work_dir: Path, name: str, message: str) -> None: ...
    def tag_points_at_head(self, work_dir: Path, name: str) -> bool: ...
    def is_working_tree_clean(self, work_dir: Path) -> bool: ...
    def uncommitted_pointers(self, work_dir: Path) -> list[str]: ...
    def uncommitted_files(self, work_dir: Path) -> list[str]: ...
    def current_commit(self, work_dir: Path) -> str: ...
    def head_commit(self, work_dir: Path) -> tuple[str, str]: ...
    def repo_toplevel(self, work_dir: Path) -> Path: ...
    def tracked_changes(self, work_dir: Path) -> list[tuple[str, str]]: ...
    def head_text(self, work_dir: Path, path: str) -> str | None: ...
    def index_text(self, work_dir: Path, path: str) -> str | None: ...
    def staged_changes(self, work_dir: Path) -> list[tuple[str, str]]: ...
    def head_files(self, work_dir: Path) -> list[str]: ...
    def has_commit(self, work_dir: Path) -> bool: ...
    def head_paths(self, work_dir: Path) -> list[str]: ...
    def commit_paths(self, work_dir: Path, paths: list[str], message: str) -> None: ...
    def branch_info(self, work_dir: Path) -> BranchInfo: ...
    def push_to(self, work_dir: Path, remote: str, ref: str) -> None: ...
    def remote_default_branch(self, work_dir: Path, remote: str) -> str | None: ...
    def open_pr(
        self,
        repo_dir: Path,
        *,
        title: str,
        body: str,
        base: str = "main",
        head: str | None = None,
    ) -> int: ...
    def pr_exists_for_branch(self, repo_dir: Path, branch: str) -> int | None: ...
    def require_github_access(self, work_dir: Path, remote: str = "origin", *, push: bool = False) -> None:
        """Before anything is written: GitHub access for where ``remote``
        fetches from, or with ``push`` for every URL it pushes to."""
        ...


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------


class SubprocessRegistryGitOps:
    """Production: shells out to `git` and `gh`.

    Each method surfaces non-zero exits as the typed exceptions defined above.
    Stdout/stderr capture is on by default for both diagnostics (in errors) and
    parsing (for `gh pr list`).
    """

    def __init__(
        self,
        *,
        timeouts: Any = None,
        reporter: Any = None,
    ) -> None:
        self._timeouts = timeouts
        self._reporter = reporter

    @property
    def _fast_timeout(self) -> float | None:
        """Wall-clock cap for fast git/gh ops (fetch, commit, tag, gh pr list).
        Falls back to a sensible 30s when no Timeouts is supplied."""
        return self._timeouts.fast if self._timeouts is not None else 30.0

    # ------------------------------------------------------------------
    # git
    # ------------------------------------------------------------------

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        # ``env`` is merged over ``git_env()`` (GIT_LFS_SKIP_SMUDGE, say).
        # Slice 25: clone uses run_streaming so git's --progress output
        # reaches the terminal live. run_streaming reads chunks (not lines)
        # so \r-based progress overwrites correctly in place — single
        # updating line per phase, not a wall of text.
        # http.lowSpeedTime gives git its own dead-transfer abort.
        from ._subprocess import run_streaming

        argv: list[str] = [
            "git",
            "-c", "http.lowSpeedLimit=1000",
            "-c", "http.lowSpeedTime=300",
            "clone", "--progress",
        ]
        if shallow:
            argv.append("--depth=1")
        if branch:
            argv.extend(["--branch", branch])
        argv.extend([url, str(dest)])
        require_github_access(url)

        # Pick wall_timeout from the structured Timeouts object if present;
        # WallTimeoutExceeded propagates unwrapped per slice 25 spec.
        wall_timeout = (
            self._timeouts.transfer if self._timeouts is not None else None
        )
        try:
            r = run_streaming(
                argv,
                env={**git_env(), **(env or {})},
                wall_timeout=wall_timeout,
                reporter=self._reporter,
            )
        except FileNotFoundError as e:
            raise GitOpError(argv, "git not installed") from e
        if r.returncode != 0:
            # run_streaming strips the newline off every captured segment,
            # so joining with "" welds git's lines together
            # ("...a git repositoryfatal: Could not read...").
            stderr = "\n".join(r.stderr_lines)
            raise GitOpError(argv, stderr, hint=_unreachable_hint(url, stderr, cwd=None))

    def fetch(self, repo_dir: Path) -> None:
        self._git(["fetch", "origin"], cwd=repo_dir)

    def require_github_access(self, work_dir: Path, remote: str = "origin", *, push: bool = False) -> None:
        require_github_access(remote, cwd=work_dir, push=push)

    def reset_hard(self, repo_dir: Path, ref: str) -> None:
        self._git(["reset", "--hard", ref], cwd=repo_dir)

    def checkout(self, repo_dir: Path, ref: str, *, force: bool = False) -> None:
        args = ["checkout"]
        if force:
            args.append("-f")
        args.append(ref)
        self._git(args, cwd=repo_dir)

    def checkout_new_branch(self, repo_dir: Path, branch: str) -> None:
        # Use ``-B`` (force-create-or-reset) so retries from a stuck
        # ``register/<name>`` state don't crash with "branch already
        # exists". Safe because the caller has just reset to a clean
        # origin/main baseline.
        self._git(["checkout", "-B", branch], cwd=repo_dir)

    def remote_branch_exists(self, repo_dir: Path, branch: str) -> bool:
        """Whether the registry already has ``branch``.

        Asked of the remote, not of local refs: the registry cache is a
        ``--depth=1`` (single-branch) clone, so local refs only ever know
        about ``main``.
        """
        out = self._git(
            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=repo_dir,
        )
        return bool(out.strip())

    def checkout_remote_branch(self, repo_dir: Path, branch: str) -> None:
        """Point the local ``branch`` at the registry's copy of it.

        The refspec is explicit on purpose: the registry cache is a
        ``--depth=1`` clone (see ``clone``), which implies ``--single-branch``,
        so a plain ``fetch origin`` only ever updates ``origin/main`` and a
        branch an earlier publish pushed is invisible locally.

        ``reset --hard`` rather than ``checkout -B <branch> FETCH_HEAD``: the
        cache carries an untracked ``.mintd_pending.json`` that the branch
        tracks, and checkout refuses to overwrite untracked files while reset
        clobbers them (the same trade ``ensure_fresh`` already makes).
        """
        self._git(["fetch", "origin", branch], cwd=repo_dir)
        self._git(["checkout", "-B", branch], cwd=repo_dir)
        self._git(["reset", "--hard", "FETCH_HEAD"], cwd=repo_dir)

    def commit_all(self, repo_dir: Path, message: str) -> None:
        self._git(["add", "-A"], cwd=repo_dir)
        self._git(["commit", "-m", message], cwd=repo_dir)

    def index_entries(self, work_dir: Path, names: list[str]) -> dict[str, str]:
        """What git's index holds for each of ``names`` (files in
        ``work_dir``), as the ``mode,object`` pair `restore_index` puts back.
        A name the index does not hold is absent. ``:(literal)``, so a name
        with ``[`` or ``*`` in it cannot match a sibling."""
        out = self._git(
            ["ls-files", "--stage", "-z", "--", *(f":(literal){n}" for n in names)], cwd=work_dir
        )
        entries: dict[str, str] = {}
        for rec in out.split("\0"):
            if rec:
                meta, name = rec.split("\t", 1)
                mode, obj, _stage = meta.split()
                entries[name] = f"{mode},{obj}"
        return entries

    def ignored_files(self, work_dir: Path, paths: list[str]) -> list[str]:
        """The files at or under ``paths`` that git ignores and doesn't track:
        what `git status` never lists and `git add -A` never picks up. Paths
        are relative to ``work_dir``, as given. Empty for no ``paths``: with no
        pathspec git would list every ignored file in the repository."""
        if not paths:
            return []
        out = self._git(
            ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--",
             *(f":(literal){p}" for p in paths)],
            cwd=work_dir,
        )
        return [name for name in out.split("\0") if name]

    def listed_files(self, work_dir: Path, paths: list[str]) -> list[str]:
        """Every entry git lists at or under ``paths``: tracked files and
        untracked ones, ignored included. A symlink is one entry, and a
        nested repository one ``dir/`` entry, since git doesn't look inside
        either. Empty for no ``paths``, as `ignored_files`."""
        if not paths:
            return []
        out = self._git(
            ["ls-files", "-z", "--cached", "--others", "--", *(f":(literal){p}" for p in paths)],
            cwd=work_dir,
        )
        return [name for name in out.split("\0") if name]

    def restore_index(self, work_dir: Path, names: list[str], entries: dict[str, str]) -> None:
        """Put git's index for ``names`` back to ``entries`` (from
        `index_entries`): a name it held gets that entry back, a name it did
        not is dropped from the index. The working tree is not touched.

        This undoes dvc's ``core.autostage``, which `git add`s the pointer and
        `.gitignore` it writes. ``--cacheinfo`` takes a path from the repo
        root, unlike update-index's plain paths, hence the prefix.
        ``--force-remove`` rather than ``rm --cached``, which refuses an entry
        that matches neither the file (already put back) nor HEAD."""
        prefix = self._git(["rev-parse", "--show-prefix"], cwd=work_dir).strip()
        for name in names:
            if name in entries:
                self._git(
                    ["update-index", "--add", "--cacheinfo", f"{entries[name]},{prefix}{name}"],
                    cwd=work_dir,
                )
            else:
                self._git(["update-index", "--force-remove", "--", name], cwd=work_dir)

    def push_branch(self, repo_dir: Path, branch: str) -> None:
        try:
            self._git(["push", "-u", "origin", branch], cwd=repo_dir)
        except GitOpError as e:
            # Two wordings for the same thing: full clones say
            # "(non-fast-forward)", shallow/single-branch clones (which the
            # registry cache is) say "(fetch first)". Deliberately narrow —
            # hook / protected-branch rejections stay plain GitOpErrors.
            if "non-fast-forward" in e.stderr or "fetch first" in e.stderr:
                raise RegistryBranchExists(e.command, e.stderr, branch) from e
            raise

    def push_with_tags(self, work_dir: Path) -> None:
        """Push the current branch and the annotated tags reachable from it:
        origin takes all of them or none.

        ``--follow-tags`` sends the tag: ``tag`` creates annotated tags, which
        a plain push leaves behind. A product cannot be advertised in the
        catalog at a rev the remote does not have, because `publish` does this
        before it writes the entry.

        ``--atomic`` keeps the two together. Git updates each ref on its own,
        so without it a branch refused as behind (a teammate pushed first)
        still lets the tag in, public on a commit the remote branch never got.
        A remote without atomic push support fails the push instead.

        Not ``push_branch``: the caller is a producer project, not the
        registry cache, and it does not know its own branch name. ``HEAD``
        resolves to the checked-out branch and creates it on the remote if
        it is not there yet.
        """
        self._git(["push", "--atomic", "--follow-tags", "origin", "HEAD"], cwd=work_dir)

    def tag_points_at_head(self, work_dir: Path, name: str) -> bool:
        """Is ``name`` an existing tag on the current commit?

        Asked after ``tag`` reports a collision, to tell an idempotent rerun
        (the tag a previous attempt left at this very commit) from a real one
        (a tag of the same name somewhere else), and by `publish` before it
        re-publishes a version the catalog already lists, as the sign of such
        a rerun. Tag names cannot contain whitespace, so ``split()`` is an
        exact match.
        """
        return name in self._git(["tag", "--points-at", "HEAD"], cwd=work_dir).split()

    def tag(self, work_dir: Path, name: str, message: str) -> None:
        try:
            self._git(["tag", "-a", name, "-m", message], cwd=work_dir)
        except GitOpError as e:
            if "already exists" in e.stderr:
                raise GitTagAlreadyExists(name, str(work_dir)) from e
            raise GitTagError(e.command, e.stderr) from e

    def is_working_tree_clean(self, work_dir: Path) -> bool:
        stdout = self._git(["status", "--porcelain"], cwd=work_dir)
        return stdout.strip() == ""

    def uncommitted_pointers(self, work_dir: Path) -> list[str]:
        """The `.dvc` / `dvc.lock` files anywhere in the repo that differ
        from HEAD: what `data push` reports. The ``:(top,glob)`` pathspecs
        match root-level files from any subdirectory and leave out
        ``.dvc/config`` and ``foodvc.lock``. ``--untracked-files=all`` keeps
        each untracked pointer a path, never a collapsed directory (git 2.48
        already descends for a file pathspec; this does not rely on it)."""
        return self._uncommitted(work_dir, _POINTER_PATHSPEC, untracked="all")

    def uncommitted_files(self, work_dir: Path) -> list[str]:
        """Everything `git status` shows: tracked changes and untracked files
        not ignored, an untracked directory as one ``dir/`` entry. `registry
        register` refuses on any, as `publish` does, because importers read
        the commit and no list of the files they need stays complete."""
        return self._uncommitted(work_dir, (), untracked="normal")

    def _uncommitted(self, work_dir: Path, pathspec: tuple[str, ...], *, untracked: str) -> list[str]:
        """Files matching ``pathspec`` (all, if empty) that differ from HEAD:
        modified, staged, deleted or untracked. Paths are relative to the
        repo root (porcelain v1 ignores ``status.relativePaths``).

        ``-z`` returns paths raw rather than C-quoted, so ``data/sp ace.dvc``
        survives; ``--no-renames`` makes a ``git mv`` one path per record and
        overrides ``status.renames``; an explicit ``--untracked-files``
        overrides ``status.showUntrackedFiles=no``.
        """
        out = self._git(
            ["status", "--porcelain", "-z", "--no-renames", f"--untracked-files={untracked}", "--", *pathspec],
            cwd=work_dir,
        )
        # dict.fromkeys: `git rm --cached` lists a path twice (staged delete,
        # then untracked).
        return list(dict.fromkeys(rec[3:] for rec in out.split("\0") if rec))

    def current_commit(self, work_dir: Path) -> str:
        return self._git(["rev-parse", "--short=7", "HEAD"], cwd=work_dir).strip()

    def head_commit(self, work_dir: Path) -> tuple[str, str]:
        """HEAD's full sha and its committer date (ISO 8601)."""
        sha, _, date = self._git(["log", "-1", "--format=%H%x00%cI"], cwd=work_dir).strip().partition("\0")
        return sha, date

    def repo_toplevel(self, work_dir: Path) -> Path:
        """The top level of the repository ``work_dir`` is in."""
        return Path(self._git(["rev-parse", "--show-toplevel"], cwd=work_dir).strip())

    def tracked_changes(self, work_dir: Path) -> list[tuple[str, str]]:
        """``(XY, path)`` for every tracked file that differs from HEAD, staged
        or not, staged-new files included; untracked files are left out. Paths
        are repo-relative, as in ``_uncommitted``."""
        out = self._git(
            ["status", "--porcelain", "-z", "--no-renames", "--untracked-files=no"], cwd=work_dir
        )
        return [(rec[:2], rec[3:]) for rec in out.split("\0") if rec]

    def head_text(self, work_dir: Path, path: str) -> str | None:
        """``path`` (repo-relative) as committed at HEAD; None when HEAD does
        not hold it. Any other failure raises."""
        try:
            return self._git(["show", f"HEAD:{path}"], cwd=work_dir)
        except GitOpError as e:
            if "does not exist in" in e.stderr or "but not in" in e.stderr:
                return None
            raise

    def index_text(self, work_dir: Path, path: str) -> str | None:
        """``path`` (relative to ``work_dir``) as staged in the index; None when
        the index does not hold it. Any other failure raises."""
        try:
            return self._git(["show", f":./{path}"], cwd=work_dir)
        except GitOpError as e:
            # `:./x` missing from the index: "unknown revision or path not in the working tree"
            if "not in the working tree" in e.stderr or "does not exist" in e.stderr:
                return None
            raise

    def staged_changes(self, work_dir: Path) -> list[tuple[str, str]]:
        """``(status, path)`` for each path under ``work_dir`` the index changes
        against HEAD (``A``, ``M``, ``D``, ...), relative to ``work_dir``; a
        rename shows as a delete and an add. Before the first commit, every
        staged path is an add."""
        # -z: unquoted names, so a path with a quote, backslash or tab reads as itself
        out = self._git(["diff", "--cached", "--name-status", "--no-renames", "--relative", "-z"], cwd=work_dir)
        parts = out.split("\0")
        return [(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]

    def head_files(self, work_dir: Path) -> list[str]:
        """Every path under ``work_dir`` the HEAD commit tracks, relative to
        ``work_dir`` (``ls-tree`` lists the current folder by default)."""
        out = self._git(["ls-tree", "-r", "--name-only", "-z", "HEAD"], cwd=work_dir)
        return [name for name in out.split("\0") if name]

    def has_commit(self, work_dir: Path) -> bool:
        """Whether HEAD names a readable commit. False only for a branch with
        no commit yet: HEAD names a branch that has no ref at all. Anything
        else (a broken or empty ref, a missing object, not a repository, git
        missing, a timeout) raises, so a check that can't read the repository
        can't pass. Asked of git's exit codes, not its messages."""
        try:
            self._git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=work_dir)
            return True
        except GitOpError as e:
            cause = e.__cause__
            if not (isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1):
                raise
        branch = self._git(["symbolic-ref", "-q", "HEAD"], cwd=work_dir).strip()  # a broken ref fails here
        # for-each-ref matches a prefix too (`feat` lists `feat/x`): compare names exactly
        if branch in self._git(["for-each-ref", "--format=%(refname)", branch], cwd=work_dir).splitlines():
            raise GitOpError(["git", "rev-parse", "--verify", "HEAD^{commit}"], f"HEAD names {branch}, which isn't a commit")
        return False

    def head_paths(self, work_dir: Path) -> list[str]:
        """The repo-relative paths the HEAD commit changed (against its first
        parent; every path for a root commit)."""
        out = self._git(
            ["-c", "core.quotepath=false", "diff-tree", "--no-commit-id", "--name-only", "-r",
             "--root", "HEAD"],
            cwd=work_dir,
        )
        return [line for line in out.splitlines() if line]

    def commit_paths(self, work_dir: Path, paths: list[str], message: str) -> None:
        """Commit exactly ``paths``: ``git commit -- <paths>`` records only
        those, so anything else already staged stays staged and out of the
        commit, and untracked files are never added. ``--literal-pathspecs``
        keeps a ``[`` or ``*`` in a name literal. Hooks run, so the transfer
        timeout applies rather than the fast cap."""
        self._git(["--literal-pathspecs", "add", "--", *paths], cwd=work_dir)
        self._git(["--literal-pathspecs", "commit", "-m", message, "--", *paths], cwd=work_dir, slow=True)

    def branch_info(self, work_dir: Path) -> BranchInfo:
        try:
            branch = self._git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=work_dir).strip()
        except GitOpError:
            return BranchInfo(None, None, None, None)
        try:
            remote = self._git(["config", "--get", f"branch.{branch}.remote"], cwd=work_dir).strip()
            merge_ref = self._git(["config", "--get", f"branch.{branch}.merge"], cwd=work_dir).strip()
        except GitOpError:
            remote = merge_ref = ""
        if not remote or not merge_ref or remote == ".":
            return BranchInfo(branch, None, None, self._count(work_dir, ["HEAD", "--not", "--remotes"]))
        if merge_ref.removeprefix("refs/heads/") != branch:
            # Not where `git push` sends this branch (push.default=simple), so
            # "ahead of it" says nothing about whether the commits are pushed.
            return BranchInfo(branch, remote, merge_ref, self._count(work_dir, ["HEAD", "--not", "--remotes"]))
        return BranchInfo(branch, remote, merge_ref, self._count(work_dir, ["@{u}..HEAD"]))

    def _count(self, work_dir: Path, revs: list[str]) -> int | None:
        try:
            return int(self._git(["rev-list", "--count", *revs], cwd=work_dir).strip())
        except (GitOpError, ValueError):
            return None

    def push_to(self, work_dir: Path, remote: str, ref: str) -> None:
        """``git push <remote> HEAD:<ref>``. The explicit refspec means
        ``push.default``, ``pushRemote`` and ``remote.<n>.push`` cannot send it
        elsewhere. Never fetches, pulls, rebases or forces. A non-fast-forward
        rejection raises ``GitPushRejected``; hook and protected-branch
        rejections stay plain ``GitOpError`` (the same narrow test as
        ``push_branch``). ``GIT_TERMINAL_PROMPT=0``: an HTTPS credential prompt
        fails at once instead of waiting on a terminal nobody is watching."""
        try:
            self._git(["push", remote, f"HEAD:{ref}"], cwd=work_dir, slow=True, env={"GIT_TERMINAL_PROMPT": "0"})
        except GitOpError as e:
            if "non-fast-forward" in e.stderr or "fetch first" in e.stderr:
                raise GitPushRejected(e.command, e.stderr) from e
            raise

    def remote_default_branch(self, work_dir: Path, remote: str) -> str | None:
        """The branch ``remote``'s HEAD names, the one importers read; None when
        git cannot say. Asks the remote, so it works where
        ``refs/remotes/<remote>/HEAD`` was never set."""
        try:
            out = self._git(["ls-remote", "--symref", remote, "HEAD"], cwd=work_dir, env={"GIT_TERMINAL_PROMPT": "0"})
        except GitOpError:
            return None
        for line in out.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                return line[len("ref: refs/heads/"):-len("\tHEAD")]
        return None

    # ------------------------------------------------------------------
    # gh
    # ------------------------------------------------------------------

    def open_pr(
        self,
        repo_dir: Path,
        *,
        title: str,
        body: str,
        base: str = "main",
        head: str | None = None,
    ) -> int:
        # Pass ``--head <branch>`` explicitly when known. gh CLI's
        # auto-detect uses the current branch's upstream tracking, which
        # can be missing (e.g. when push -u set tracking but a different
        # remote is what gh resolves the repo to). Surfacing head
        # explicitly removes the ambiguity that caused the
        # "must first push the current branch" error.
        args = ["pr", "create", "--title", title, "--body", body, "--base", base]
        if head is not None:
            args.extend(["--head", head])
        try:
            result = self._gh(args, cwd=repo_dir)
        except PRConflictError as e:
            # `_gh` is generic and cannot know the branch; this call site can.
            raise PRConflictError(branch=head or "(unknown)", existing_pr=e.existing_pr) from e
        # `gh pr create` prints the PR URL on success. Extract the trailing integer.
        url = result.strip().splitlines()[-1]
        try:
            return int(url.rsplit("/", 1)[-1])
        except (IndexError, ValueError) as e:
            raise GitOpError(["gh", "pr", "create"], f"could not parse PR number from: {url}") from e

    def pr_exists_for_branch(self, repo_dir: Path, branch: str) -> int | None:
        result = self._gh(
            ["pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            cwd=repo_dir,
        )
        items = json.loads(result.strip() or "[]")
        if not items:
            return None
        return int(items[0]["number"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git(
        self, args: list[str], *, cwd: Path | None, slow: bool = False, env: dict[str, str] | None = None,
    ) -> str:
        """``slow`` swaps the fast cap for the transfer timeout (unbounded by
        default), for a commit whose hooks or a push whose remote can take
        longer than 30 s. ``env`` is merged over ``git_env()``."""
        cmd = ["git", *args]
        network = _network_target(args)
        if network is not None:  # every call that reaches a remote, present or future
            for remote in [network[1]] if network[1] is not None else _every_remote(args, cwd):
                require_github_access(remote, cwd=cwd, push=network[0] == "push")
        timeout = (self._timeouts.transfer if self._timeouts is not None else None) if slow else self._fast_timeout
        try:
            # git writes paths as raw bytes under `-z` (UTF-8 for any name it
            # did not get from elsewhere): decode them as UTF-8 whatever the
            # locale (cp1252 on Windows). A byte that is not UTF-8 becomes a
            # surrogate rather than a traceback or a U+FFFD: on POSIX it encodes
            # back to the same byte in argv, so a path read here and handed to
            # `update-index` names the same file.
            result = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=timeout,
                env={**git_env(), **(env or {})},
                check=True,
            )
        except FileNotFoundError as e:
            raise GitOpError(cmd, "git not installed") from e
        except subprocess.TimeoutExpired as e:
            raise GitOpError(cmd, f"timed out after {timeout}s") from e
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or e.stdout or ""
            hint = (
                _unreachable_hint(network[1], stderr, cwd=cwd, push=network[0] == "push")
                if network is not None and network[1] is not None else None
            )
            raise GitOpError(cmd, stderr, hint=hint) from e
        return result.stdout

    def _gh(self, args: list[str], *, cwd: Path) -> str:
        cmd = ["gh", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._fast_timeout,
                env=git_env(),
                check=True,
            )
        except FileNotFoundError as e:
            raise GhNotInstalled(str(e)) from e
        except subprocess.TimeoutExpired as e:
            raise GitOpError(cmd, f"timed out after {self._fast_timeout}s") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "") + (e.stdout or "")
            if "not authenticated" in stderr.lower() or "authentication" in stderr.lower():
                raise GhAuthError(stderr) from e
            if "already exists" in stderr.lower():
                raise PRConflictError(branch="(unknown)") from e
            raise GitOpError(cmd, stderr) from e
        return result.stdout
