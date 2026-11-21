"""mintd reaches GitHub through gh only, checked in the git seams before git
reaches github.com, with one hint that names the account to use.

The real check runs here, against a stub ``gh`` first on PATH, an isolated git
config and a proxy on a closed port, so nothing reaches the network. Every
other test sees gh as ready (``conftest._github_access_is_hermetic``); the
fixture below puts the real functions back. Org and account names are made up.
"""

from __future__ import annotations

import os
import socket
import subprocess
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from mintd import _producer_git_ops, _registry_git_ops, cli
from mintd._config import Timeouts
from mintd._dvc_ops import DvcImportRepoUnreachable, SubprocessDvcOps
from mintd._registry_git_ops import (
    NETWORK_TOO,
    SSH_TO_HTTPS,
    GitHubAccessMissing,
    GitOpError,
    SubprocessRegistryGitOps,
    _unreachable_hint,
    github_access_hint,
    require_github_access,
)
from mintd.producer import FetchError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stub gh is a POSIX script")

_REAL_GH_STATE = _registry_git_ops._gh_state
_REAL_URL_GIT_USES = _registry_git_ops.url_git_uses
_REPO = "https://github.com/example-lab/demo"
_HELPER = "!gh auth git-credential"

_STUB_GH = """#!/bin/sh
echo "gh $*" >> "$GH_STUB_LOG"
if [ -n "$GH_STUB_OLD" ]; then echo "unknown flag: --hostname" >&2; exit 1; fi  # gh before 2.17
if [ -s "$GH_STUB_TOKEN" ] || [ -n "$GH_STUB_EXIT0" ]; then cat "$GH_STUB_TOKEN"; exit 0; fi
echo "no oauth token found for github.com" >&2; exit 1
"""


class GitHub:
    """The stub gh's token and hosts.yml, and the isolated git config, set per test."""

    def __init__(self, tmp_path: Path) -> None:
        self.token = tmp_path / "gh-token"
        self.config = tmp_path / "gh-config"
        self.log = tmp_path / "gh.log"
        self.gitconfig = tmp_path / "gitconfig"
        self.config.mkdir()
        self.log.touch()
        self.gitconfig.touch()
        self.accounts()

    def accounts(self, active: str | None = None, *others: str) -> None:
        """gh logged in as ``active`` (a token and hosts.yml's user), with ``others`` beside it."""
        self.token.write_text("gho_stub\n" if active else "")
        users = "".join(f"            {u}: {{}}\n" for u in ([active] if active else []) + list(others))
        hosts = f"github.com:\n    users:\n{users}    user: {active}\n" if active else "{}\n"
        (self.config / "hosts.yml").write_text(hosts)
        _REAL_GH_STATE.cache_clear()

    def git_config(self, key: str, value: str) -> None:
        subprocess.run(["git", "config", "--file", str(self.gitconfig), "--add", key, value], check=True)
        _REAL_GH_STATE.cache_clear()

    def ready(self, active: str = "alice-lab", *others: str) -> None:
        self.accounts(active, *others)
        self.git_config("credential.https://github.com.helper", _HELPER)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GitHub]:
    gh = GitHub(tmp_path)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "gh").write_text(_STUB_GH)
    (stubs / "gh").chmod(0o755)
    with socket.socket() as s:  # a port nothing listens on: any network call fails fast
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_STUB_TOKEN", str(gh.token))
    monkeypatch.setenv("GH_STUB_LOG", str(gh.log))
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh.config))
    monkeypatch.delenv("GH_STUB_OLD", raising=False)
    monkeypatch.delenv("GH_STUB_EXIT0", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gh.gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{closed}")
    monkeypatch.setattr(_registry_git_ops, "_gh_state", _REAL_GH_STATE)
    monkeypatch.setattr(_registry_git_ops, "url_git_uses", _REAL_URL_GIT_USES)
    _REAL_GH_STATE.cache_clear()
    yield gh
    _REAL_GH_STATE.cache_clear()


def _refused(url: str) -> str:
    with pytest.raises(GitHubAccessMissing) as raised:
        require_github_access(url)
    return raised.value.hint or ""


# --- the check -------------------------------------------------------------


@pytest.mark.parametrize("helper", [False, True])
def test_not_logged_in_is_refused(github: GitHub, helper: bool) -> None:
    if helper:  # git would use gh, but gh has no account
        github.git_config("credential.https://github.com.helper", _HELPER)
    assert "gh auth login" in _refused(_REPO)


def test_logged_in_but_git_not_using_gh_is_refused(github: GitHub) -> None:
    github.accounts("alice-lab")
    assert "gh auth setup-git" in _refused(_REPO)


@pytest.mark.parametrize("url", ["git@github.com:example-lab/demo.git", "ssh://git@github.com/example-lab/demo"])
def test_an_ssh_url_is_refused_with_the_lines_that_rewrite_it(github: GitHub, url: str) -> None:
    github.ready()
    assert SSH_TO_HTTPS in _refused(url)


def test_https_rewritten_to_ssh_is_refused_with_the_rule_to_remove(github: GitHub) -> None:
    github.ready()
    github.git_config('url.git@github.com:.insteadOf', "https://github.com/")
    assert "remove that rule" in _refused(_REPO)  # the insteadOf lines alone don't undo it


def test_ssh_rewritten_to_https_passes_once_gh_is_set_up(github: GitHub) -> None:
    github.ready()
    github.git_config('url.https://github.com/.insteadOf', "git@github.com:")
    require_github_access("git@github.com:example-lab/demo.git")


@pytest.mark.parametrize("url", ["/srv/git/demo.git", "https://gitlab.example/example-lab/demo"])
def test_a_local_path_or_another_host_never_asks_gh(github: GitHub, url: str) -> None:
    require_github_access(url)
    assert github.calls() == []


def test_gh_is_asked_once_per_process(github: GitHub) -> None:
    github.ready()
    require_github_access(_REPO)
    require_github_access("https://github.com/example-lab/other")
    assert github.calls() == ["gh auth token --hostname github.com"]


def test_no_gh_says_to_install_it(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    only_git = tmp_path / "only-git"  # git and nothing else, even on a runner with gh in /usr/bin
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git") or "git")
    monkeypatch.setenv("PATH", str(only_git))
    assert "install gh" in _refused(_REPO)


def test_a_gh_too_old_for_auth_token_says_to_upgrade_gh(github: GitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    github.ready()
    monkeypatch.setenv("GH_STUB_OLD", "1")
    _REAL_GH_STATE.cache_clear()
    hint = _refused(_REPO)
    assert "gh 2.17 or newer" in hint and "gh auth login" not in hint


def test_an_empty_token_is_not_a_login(github: GitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    github.git_config("credential.https://github.com.helper", _HELPER)
    monkeypatch.setenv("GH_STUB_EXIT0", "1")  # `gh auth token` exits 0 but prints nothing
    assert "gh auth login" in _refused(_REPO)


def test_the_account_name_never_decides(github: GitHub) -> None:
    """Only `gh auth token` says logged in; hosts.yml's account is for the hint."""
    github.ready("alice-lab")
    github.token.write_text("")  # gh's config names an account, but gh has no token
    _REAL_GH_STATE.cache_clear()
    assert "gh auth login" in _refused(_REPO)
    github.ready("alice-lab")
    (github.config / "hosts.yml").unlink()  # a token, but no readable account name
    _REAL_GH_STATE.cache_clear()
    require_github_access(_REPO)
    assert github_access_hint(_REPO) == (
        "this repo is in example-lab; check that your active gh account can see it: `gh auth status`, then `gh auth switch`"
    )


def test_gh_token_in_the_environment_hides_the_account_name(github: GitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    github.ready("bob-personal", "alice-lab")
    monkeypatch.setenv("GH_TOKEN", "from-the-environment")  # it picks the account, not hosts.yml
    _REAL_GH_STATE.cache_clear()
    assert "bob-personal" not in github_access_hint(_REPO)


# --- the hint ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("others", "switch"),
    [
        (("alice-lab",), "run `gh auth switch -u alice-lab`"),
        (("alice-lab", "carol-lab"), "switch to one of alice-lab, carol-lab with `gh auth switch -u <account>`"),
        ((), "log in with an account in example-lab: `gh auth login`"),
    ],
)
def test_the_hint_names_the_org_the_active_account_and_the_switch(
    github: GitHub, others: tuple[str, ...], switch: str
) -> None:
    github.ready("bob-personal", *others)
    assert github_access_hint(_REPO) == f"this repo is in example-lab; your active gh account is bob-personal; {switch}"


def test_an_unreachable_github_producer_gets_the_account_hint(github: GitHub) -> None:
    from mintd.errors import unreachable_repo_hint

    github.ready("bob-personal", "alice-lab")
    hint = unreachable_repo_hint(_REPO)
    assert "your active gh account is bob-personal; run `gh auth switch -u alice-lab`" in hint
    assert NETWORK_TOO in hint  # the same failure can be the network
    assert "if it moved" in hint


def test_github_hiding_a_repo_from_the_account_gets_the_account_hint(github: GitHub) -> None:
    """GitHub answers "Repository not found" when the active account can't see
    a private repo: the case the account hint is for."""
    github.ready("bob-personal", "alice-lab")
    stderr = "remote: Repository not found.\nfatal: repository 'https://github.com/example-lab/demo/' not found"
    hint = _unreachable_hint(_REPO, stderr, cwd=None) or ""
    assert "run `gh auth switch -u alice-lab`" in hint
    assert NETWORK_TOO in hint


def test_no_ssh_t_advice_anywhere_in_mintd() -> None:
    """A user without an SSH key was told to run `ssh -T`; gh is the one way now."""
    src = Path(cli.__file__).parent
    offenders = [p.name for p in src.rglob("*") if p.is_file() and "ssh -T" in p.read_text(encoding="utf-8", errors="replace")]
    assert offenders == []


# --- the seams ---------------------------------------------------------------


def _local_repo(tmp_path: Path, origin: str) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    return repo


def test_registry_clone_is_checked_before_git_runs(github: GitHub, tmp_path: Path) -> None:
    with pytest.raises(GitHubAccessMissing):
        SubprocessRegistryGitOps().clone(_REPO, tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


@pytest.mark.parametrize(
    "call",
    [
        # A future method's call through the seam, with no check of its own.
        lambda ops, repo: ops._git(["fetch", "origin"], cwd=repo),
        lambda ops, repo: ops._git(["ls-remote", "--heads", "origin"], cwd=repo),
        lambda ops, repo: ops.push_to(repo, "origin", "main"),
    ],
)
def test_any_network_call_through_the_registry_seam_is_checked(
    github: GitHub, tmp_path: Path, call: Callable[[SubprocessRegistryGitOps, Path], object]
) -> None:
    repo = _local_repo(tmp_path, _REPO)
    # A GitOpError, so every caller's existing recovery (publish's
    # ProjectPushFailed, data push's push_failed) handles it.
    with pytest.raises(GitOpError) as raised:
        call(SubprocessRegistryGitOps(), repo)
    assert isinstance(raised.value, GitHubAccessMissing)


def test_a_local_git_call_never_asks_gh(github: GitHub, tmp_path: Path) -> None:
    repo = _local_repo(tmp_path, _REPO)
    SubprocessRegistryGitOps()._git(["status", "--porcelain"], cwd=repo)
    assert github.calls() == []


def test_any_producer_git_call_is_checked(github: GitHub) -> None:
    """Refused as this seam's own error, which its callers already handle."""
    with pytest.raises(FetchError) as raised:
        _producer_git_ops._run(["git", "ls-remote", _REPO, "HEAD"], timeout=5, repo=_REPO, pin="", binary_stdout=False)
    assert raised.value.reason is FetchError.Reason.UNREACHABLE
    assert "isn't ready" in (raised.value.detail or "")


def test_dvc_import_is_checked_before_dvc_runs(github: GitHub, tmp_path: Path) -> None:
    """Refused as this seam's own error, which its callers already handle."""
    with pytest.raises(DvcImportRepoUnreachable) as raised:
        SubprocessDvcOps(timeouts=Timeouts()).import_(repo_url=_REPO, path="data", dest=tmp_path / "out", cwd=tmp_path)
    assert "gh auth login" in (raised.value.hint or "")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("catalog_url", "rewrite"),
    [
        (_REPO, ("url.https://x-access-token:SECRET@github.com/.insteadOf", "https://github.com/")),
        ("git@github.com:example-lab/demo.git", ("url.https://github.com/.insteadOf", "git@github.com:")),
    ],
)
def test_dvc_gets_the_catalog_url_not_the_local_rewrite(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_url: str, rewrite: tuple[str, str]
) -> None:
    """dvc writes the URL it is given into the pointer, which gets committed:
    never a token or a mirror from the user's own git config. An SSH catalog
    URL goes as https, since dvc's own clone may not apply insteadOf."""
    github.ready()
    github.git_config(*rewrite)
    spawned: list[list[str]] = []

    class Spawned(Exception):
        pass

    def spawn(self: SubprocessDvcOps, cmd: list[str], **kwargs: object) -> None:
        spawned.append(cmd)
        raise Spawned

    monkeypatch.setattr(SubprocessDvcOps, "_spawn", spawn)
    with pytest.raises(Spawned):
        SubprocessDvcOps(timeouts=Timeouts()).import_(repo_url=catalog_url, path="data", dest=tmp_path / "out", cwd=tmp_path)
    cmd = spawned[0]
    assert cmd[cmd.index("import") + 1] == _REPO
    assert not any("SECRET" in arg for arg in cmd)


def test_the_refusal_never_carries_a_token(github: GitHub) -> None:
    github.git_config("url.https://x-access-token:SECRET@github.com/.insteadOf", "https://github.com/")
    with pytest.raises(GitHubAccessMissing) as raised:
        require_github_access(_REPO)
    assert "SECRET" not in f"{raised.value} {raised.value.stderr} {raised.value.hint}"


@pytest.mark.parametrize("gh_ready", [False, True], ids=["refused-before-git", "git-runs-and-fails"])
def test_a_token_in_an_insteadof_rewrite_is_never_printed(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], gh_ready: bool
) -> None:
    """The URL git uses carries the user's token: the refusal names it, and so
    can git's own error. Neither may print the token."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        f"registry_url: https://github.com/example-lab/registry\ncache_dir: {tmp_path / 'cache'}\n"
    )
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(cfg))
    github.git_config("url.https://x-access-token:SECRET@github.com/.insteadOf", "https://github.com/")
    if gh_ready:
        github.ready()  # git runs, and fails on the closed-port proxy
    else:
        github.accounts("bob-personal")  # git doesn't use gh: refused before git runs
    assert cli.main(["data", "list"]) == 1
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    assert "example-lab/registry" in err


def test_data_list_stops_with_the_hint(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        f"registry_url: https://github.com/example-lab/registry\ncache_dir: {tmp_path / 'cache'}\n"
    )
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(cfg))
    github.accounts("bob-personal")
    assert cli.main(["data", "list"]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "gh auth setup-git" in err


# --- one repo identity -------------------------------------------------------


def test_an_import_made_through_an_ssh_form_catalog_url_is_the_same_producer(tmp_path: Path) -> None:
    """dvc gets the URL git uses, so the pointer records https where the catalog
    says git@github.com:. Re-import and bump (`_require_owner`) must still see
    one producer, and still refuse a different one."""
    from mintd.data import UnknownProductPath, _require_owner
    from tests._harness.consumer import Import, write_import

    dvc = write_import(
        tmp_path,
        Import(name="demo", producer_url=_REPO, pin="a" * 40, producer_path="data/final", local_path="demo"),
        under="data/imports/demo",
    )
    for catalog_url in ("git@github.com:example-lab/demo.git", "ssh://git@github.com/example-lab/demo", _REPO + ".git"):
        _require_owner(dvc, catalog_url, name="demo")
    with pytest.raises(UnknownProductPath):
        _require_owner(dvc, "git@github.com:other-lab/demo.git", name="demo")


def test_an_import_that_landed_nothing_from_an_unreachable_producer_gets_the_shared_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mintd import data
    from mintd.errors import unreachable_repo_hint
    from mintd.producer import GitArchiveFetcher
    from tests._harness.consumer import Import, write_import

    dvc = write_import(
        tmp_path,
        Import(name="demo", producer_url=_REPO, pin="a" * 40, producer_path="data/final", local_path="demo"),
        under="data/imports/demo",
    )

    def unreachable(self: GitArchiveFetcher, repo: str, pin: str, dest: Path) -> None:
        raise FetchError.unreachable(repo, pin, detail="remote: Repository not found.")

    monkeypatch.setattr(GitArchiveFetcher, "checkout_at", unreachable)
    with pytest.raises(data.UndeliverableImport) as raised:
        data._require_deliverable(_REPO, "data/final", dvc, name="demo", force=False, rev_requested=False)
    assert (raised.value.hint or "").startswith(unreachable_repo_hint(_REPO))


# --- a push is checked where git will push ------------------------------------


@pytest.mark.parametrize(
    "rule",
    [("remote.origin.pushurl", "git@github.com:example-lab/demo.git"), ("url.git@github.com:.pushInsteadOf", "https://github.com/")],
    ids=["pushurl", "pushInsteadOf"],
)
def test_a_push_is_checked_against_where_git_will_push(github: GitHub, tmp_path: Path, rule: tuple[str, str]) -> None:
    github.ready()
    repo = _local_repo(tmp_path, _REPO)
    subprocess.run(["git", "-C", str(repo), "config", *rule], check=True)
    with pytest.raises(GitHubAccessMissing) as raised:
        SubprocessRegistryGitOps().push_to(repo, "origin", "main")
    assert SSH_TO_HTTPS in (raised.value.hint or "")
    require_github_access("origin", cwd=repo)  # a fetch still goes over https


# --- the hint on a git failure, and the verbs stopping --------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda ops, repo, tmp: ops._git(["ls-remote", "origin"], cwd=repo),
        lambda ops, repo, tmp: ops.clone(_REPO, tmp / "clone"),
    ],
    ids=["_git", "clone"],
)
def test_a_git_failure_in_the_registry_seam_names_the_account_to_switch_to(
    github: GitHub, tmp_path: Path, call: Callable[[SubprocessRegistryGitOps, Path, Path], object]
) -> None:
    github.ready("bob-personal", "alice-lab")  # gh is ready: git runs, and fails on the closed-port proxy
    repo = _local_repo(tmp_path, _REPO)
    with pytest.raises(GitOpError) as raised:
        call(SubprocessRegistryGitOps(), repo, tmp_path)
    assert not isinstance(raised.value, GitHubAccessMissing)
    assert "run `gh auth switch -u alice-lab`" in (raised.value.hint or "")


def test_a_failed_push_is_described_by_where_git_pushed(github: GitHub, tmp_path: Path) -> None:
    """Fetch over SSH, push over https (a pushurl): the push passes the check,
    fails on the network, and gets the account hint for the https URL, not
    the SSH advice for the fetch URL."""
    github.ready("bob-personal", "alice-lab")
    repo = _local_repo(tmp_path, "git@github.com:example-lab/demo.git")
    subprocess.run(["git", "-C", str(repo), "config", "remote.origin.pushurl", _REPO], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    with pytest.raises(GitOpError) as raised:
        SubprocessRegistryGitOps().push_to(repo, "origin", "main")
    hint = raised.value.hint or ""
    assert "run `gh auth switch -u alice-lab`" in hint and SSH_TO_HTTPS not in hint


def test_data_clone_stops_before_git_and_says_nothing_was_cloned(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mintd.catalog import InMemoryCatalogClient
    from mintd.data import clone_and_pull_product
    from mintd.producer import ProducerError
    from tests._fakes.dvc_ops import _FakeDvcOps
    from tests.test_data_clone import _fast, _register

    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    with pytest.raises(ProducerError) as raised:
        clone_and_pull_product(client, _FakeDvcOps(), SubprocessRegistryGitOps(), _fast(), name="demo-xw")
    assert "isn't ready" in str(raised.value)
    assert "partial clone" not in str(raised.value)
    assert not (tmp_path / "data_demo-xw").exists()


def test_data_import_stops_before_dvc_with_the_hint(github: GitHub, tmp_path: Path) -> None:
    from mintd.catalog import InMemoryCatalogClient
    from mintd.data import import_product
    from mintd.errors import MintdError
    from tests.test_data_import import _register, _with_primary

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    with pytest.raises(MintdError) as raised:
        import_product(client, SubprocessDvcOps(timeouts=Timeouts()), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "isn't ready" in str(raised.value)
    assert "gh auth login" in (raised.value.hint or "")
    assert not (tmp_path / "data_demo_xw").exists()
