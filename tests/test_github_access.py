"""mintd reaches GitHub through gh only, checked in the git seams before git
reaches github.com, with one hint that names the account to use.

The real check runs everywhere; here it meets every state, against a stub
``gh`` first on PATH, an isolated git config and a proxy on a closed port, so
nothing reaches the network. Org and account names are made up.
"""

from __future__ import annotations

import os
import socket
import subprocess
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from mintd import _producer_git_ops, cli
from tests._harness.access import clear_access_caches
from mintd._config import Timeouts
from mintd._dvc_ops import DvcImportRepoUnreachable, DvcPullRefused, SubprocessDvcOps
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

# Tests here change gh's state partway through, so they read it afresh after each change.
_clear_caches = clear_access_caches
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
        _clear_caches()

    def git_config(self, key: str, value: str) -> None:
        subprocess.run(["git", "config", "--file", str(self.gitconfig), "--add", key, value], check=True)
        _clear_caches()

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
    yield gh  # conftest cleared what mintd caches before this test began


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


def test_https_rewritten_to_ssh_is_refused_with_the_rule_to_change(github: GitHub) -> None:
    github.ready()
    github.git_config('url.git@github.com:.insteadOf', "https://github.com/")
    hint = _refused(_REPO)  # the insteadOf lines alone don't undo the user's own rule
    assert "url.<base>.insteadOf" in hint and f"change it to {_REPO}" in hint


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
    _clear_caches()
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
    _clear_caches()
    assert "gh auth login" in _refused(_REPO)
    github.ready("alice-lab")
    (github.config / "hosts.yml").unlink()  # a token, but no readable account name
    _clear_caches()
    require_github_access(_REPO)
    assert github_access_hint(_REPO) == (
        "this repo is in example-lab; check that your active gh account can see it: `gh auth status`, then `gh auth switch`"
    )


def test_gh_token_in_the_environment_hides_the_account_name(github: GitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    github.ready("bob-personal", "alice-lab")
    monkeypatch.setenv("GH_TOKEN", "from-the-environment")  # it picks the account, not hosts.yml
    _clear_caches()
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
        _require_owner(dvc, catalog_url, name="demo", cwd=tmp_path)
    with pytest.raises(UnknownProductPath):
        _require_owner(dvc, "git@github.com:other-lab/demo.git", name="demo", cwd=tmp_path)


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


def test_a_token_in_the_catalog_url_never_reaches_the_written_pointer(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real dvc, a local producer reached through an insteadOf: the .dvc file
    dvc writes records the catalog URL without its credentials. gh's helper
    supplies them, and the pointer gets committed."""
    from tests._harness.producer import build_local_producer

    producer = build_local_producer(tmp_path / "prod")
    producer.publish({"data/final": {"a.csv": b"v1\n"}})
    github.accounts("alice-lab")
    home = tmp_path / "dvc-home"
    home.mkdir()
    # A default setup, in ~/.gitconfig: dvc's own clone doesn't follow
    # GIT_CONFIG_GLOBAL the way git does. The rule sends dvc to the local producer.
    (home / ".gitconfig").write_text(
        f'[credential "https://github.com"]\n\thelper = {_HELPER}\n[url "{producer.url}"]\n\tinsteadOf = {_REPO}\n'
    )
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    for key, value in {
        "HOME": str(home), "USERPROFILE": str(home), "DVC_GLOBAL_CONFIG_DIR": str(home / "global"),
        "DVC_SYSTEM_CONFIG_DIR": str(home / "system"), "DVC_SITE_CACHE_DIR": str(tmp_path / "dvc-site"),
    }.items():
        monkeypatch.setenv(key, value)
    ws = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    ops = SubprocessDvcOps(timeouts=Timeouts())
    ops.init(cwd=ws)
    ops.import_(repo_url="https://x-access-token:SECRET@github.com/example-lab/demo.git", path="data/final",
                dest=ws / "final", cwd=ws)
    pointer = (ws / "final.dvc").read_text(encoding="utf-8")
    assert "SECRET" not in pointer
    assert f"url: {_REPO}\n" in pointer


# --- one parser for every form git takes ---------------------------------------

_GITHUB_FORMS = [
    ("https://github.com/o/r", "https", "github.com"),
    ("https://GitHub.com/o/r", "https", "github.com"),
    ("https://github.com:443/o/r", "https", "github.com"),
    ("https://www.github.com/o/r", "https", "www.github.com"),
    ("https://github.com/o/r/", "https", "github.com"),
    ("https://github.com//o/r", "https", "github.com"),
    ("https://x-access-token:SECRET@github.com/o/r.git", "https", "github.com"),
    ("http://github.com/o/r", "http", "github.com"),
    ("git://github.com/o/r", "git", "github.com"),
    ("git@github.com:o/r.git", "ssh", "github.com"),
    ("git@GitHub.com:o/r.git", "ssh", "github.com"),
    ("github.com:o/r.git", "ssh", "github.com"),
    ("org-123@github.com:o/r.git", "ssh", "github.com"),
    ("ssh://git@github.com/o/r", "ssh", "github.com"),
    ("ssh://git@GITHUB.COM/o/r", "ssh", "github.com"),
    ("ssh://git@github.com:22/o/r", "ssh", "github.com"),
    ("ssh://git@ssh.github.com:443/o/r", "ssh", "ssh.github.com"),
    ("ssh://github.com/o/r", "ssh", "github.com"),
    ("git+ssh://git@github.com/o/r", "ssh", "github.com"),
    ("https://github.com./o/r", "https", "github.com"),  # a fully qualified host's trailing dot
    ("git@github.com.:o/r.git", "ssh", "github.com"),
]
_SSH_FORMS = [url for url, transport, _ in _GITHUB_FORMS if transport == "ssh"]


@pytest.mark.parametrize(("url", "transport", "host"), _GITHUB_FORMS)
def test_every_form_git_takes_reads_as_one_github_repo(url: str, transport: str, host: str) -> None:
    """github_owner, https_remote and the SSH check share this reading, so
    they can't disagree about a URL."""
    from mintd._registry_git_ops import github_owner, https_remote, parse_repo_url

    repo = parse_repo_url(url)
    assert repo is not None and (repo.transport, repo.host, repo.owner, repo.repo) == (transport, host, "o", "r")
    assert github_owner(url) == "o"
    assert https_remote(url) == "https://github.com/o/r"


def test_userinfo_is_kept_apart() -> None:
    from mintd._registry_git_ops import https_remote, parse_repo_url

    url = "https://x-access-token:SECRET@github.com/o/r.git"
    assert parse_repo_url(url).userinfo == "x-access-token:SECRET"  # type: ignore[union-attr]
    assert "SECRET" not in https_remote(url)


@pytest.mark.parametrize(
    "url",
    ["/srv/git/demo.git", "file:///srv/git/demo", "C:\\repos\\demo", "https://gitlab.example/o/r",
     "git@gitlab.example:o/r.git", "ssh://git@github.com.example/o/r", "HEAD:refs/heads/main"],
)
def test_anything_else_is_not_github(github: GitHub, url: str) -> None:
    from mintd._registry_git_ops import github_owner

    assert github_owner(url) is None
    require_github_access(url)  # nothing to check
    assert github.calls() == []


@pytest.mark.parametrize("url", [url for url, _, _ in _GITHUB_FORMS])
def test_every_github_form_is_refused_while_logged_out(github: GitHub, url: str) -> None:
    assert _refused(url)


@pytest.mark.parametrize("url", _SSH_FORMS)
def test_every_ssh_form_is_refused_even_with_gh_ready(github: GitHub, url: str) -> None:
    github.ready()
    assert SSH_TO_HTTPS in _refused(url)


@pytest.mark.parametrize("url", ["http://github.com/o/r", "git://github.com/o/r"])
def test_http_and_git_transports_are_refused_with_the_https_url(github: GitHub, url: str) -> None:
    github.ready()
    assert _refused(url) == "mintd reaches GitHub through gh, over https only: use https://github.com/o/r"


def test_https_rewritten_to_ssh_over_443_is_refused(github: GitHub) -> None:
    github.ready()
    github.git_config("url.ssh://git@ssh.github.com:443/.insteadOf", "https://github.com/")
    assert SSH_TO_HTTPS in _refused(_REPO)


# --- every URL git will push to; and failing closed ---------------------------


def test_every_push_url_is_checked(github: GitHub, tmp_path: Path) -> None:
    github.ready()
    repo = _local_repo(tmp_path, _REPO)
    for url in (_REPO, "git@github.com:example-lab/other.git"):  # the second one goes over SSH
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "--add", "--push", "origin", url], check=True)
    with pytest.raises(GitHubAccessMissing):
        require_github_access("origin", cwd=repo, push=True)
    require_github_access("origin", cwd=repo)  # fetching still goes over https


def test_a_push_to_a_url_meets_push_insteadof(github: GitHub, tmp_path: Path) -> None:
    github.ready()
    github.git_config("url.git@github.com:.pushInsteadOf", "https://github.com/")
    repo = _local_repo(tmp_path, "/srv/git/elsewhere.git")
    with pytest.raises(GitHubAccessMissing):
        require_github_access(_REPO, cwd=repo, push=True)
    require_github_access(_REPO, cwd=repo)


def test_a_url_lookup_that_times_out_is_refused(github: GitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: never read as "the remote's name", which is no GitHub URL."""
    github.ready()
    real = subprocess.run

    def run(argv: list[str], **kwargs: object) -> object:
        if argv[:2] == ["git", "ls-remote"]:
            raise subprocess.TimeoutExpired(argv, 10)
        return real(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(GitHubAccessMissing) as raised:
        require_github_access("origin")
    assert "in time" in (raised.value.hint or "")
    with pytest.raises(GitHubAccessMissing) as raised:  # the hint repeats no argv, so no password
        require_github_access("https://u:SECRET#x@github.com/example-lab/demo")
    assert "SECRET" not in f"{raised.value} {raised.value.hint}"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["fetch", "origin"], ("fetch", "origin")),
        (["fetch", "--depth=1", "origin", "abc"], ("fetch", "origin")),
        (["push", "--atomic", "--follow-tags", "origin", "HEAD"], ("push", "origin")),
        (["-c", "http.lowSpeedLimit=1000", "push", "origin", "HEAD:main"], ("push", "origin")),
        (["--no-pager", "fetch", "origin"], ("fetch", "origin")),
        (["fetch", "--depth", "1", "origin"], ("fetch", None)),  # an option value before the remote
        (["push", "-o", "ci.skip", "origin", "HEAD"], ("push", None)),
        (["fetch"], ("fetch", None)),
        (["pull", "--rebase"], ("pull", None)),
        (["remote", "update"], ("remote", None)),
        (["remote", "show", "origin"], ("remote", None)),
        (["submodule", "update", "--init"], ("submodule", None)),
        (["archive", "--remote", _REPO, "HEAD"], ("archive", _REPO)),
        (["archive", "HEAD"], None),
        (["status", "--porcelain"], None),
        (["remote", "get-url", "origin"], None),
    ],
)
def test_network_target_names_the_remote_only_when_certain(args: list[str], expected: object) -> None:
    from mintd._registry_git_ops import _network_target

    assert _network_target(args) == expected


def test_an_uncertain_target_checks_every_remote(github: GitHub, tmp_path: Path) -> None:
    github.ready()
    repo = _local_repo(tmp_path, _REPO)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "upstream", "git@github.com:example-lab/up.git"], check=True)
    with pytest.raises(GitHubAccessMissing):  # "--depth 1": not certain which remote, so all of them
        SubprocessRegistryGitOps()._git(["fetch", "--depth", "1", "origin"], cwd=repo)
    with pytest.raises(GitOpError) as raised:  # named for certain: origin only, then the network fails
        SubprocessRegistryGitOps()._git(["fetch", "origin"], cwd=repo)
    assert not isinstance(raised.value, GitHubAccessMissing)


# --- git uses gh: every helper that applies, with the repo's own config ----------


@pytest.mark.parametrize(
    ("entries", "uses_gh"),
    [
        ([("credential.helper", "osxkeychain"), ("credential.https://github.com.helper", ""),
          ("credential.https://github.com.helper", _HELPER)], True),  # what `gh auth setup-git` writes
        ([("credential.https://github.com.helper", _HELPER), ("credential.https://github.com.helper", "osxkeychain")], True),
        ([("credential.helper", _HELPER)], True),
        ([("credential.https://github.com/.helper", _HELPER)], True),
        ([("credential.https://github.com.helper", _HELPER), ("credential.helper", "")], False),  # cleared after
        ([("credential.https://gitlab.example.helper", _HELPER)], False),
        ([("credential.helper", "osxkeychain")], False),
    ],
)
def test_every_credential_helper_that_applies_is_read(
    github: GitHub, entries: list[tuple[str, str]], uses_gh: bool
) -> None:
    github.accounts("alice-lab")
    for key, value in entries:
        github.git_config(key, value)
    if uses_gh:
        require_github_access(_REPO)
    else:
        assert "gh auth setup-git" in _refused(_REPO)


def test_a_helper_in_the_repos_own_config_counts(github: GitHub, tmp_path: Path) -> None:
    github.accounts("alice-lab")
    repo = _local_repo(tmp_path, _REPO)
    subprocess.run(["git", "-C", str(repo), "config", "credential.https://github.com.helper", _HELPER], check=True)
    require_github_access("origin", cwd=repo)
    assert "gh auth setup-git" in _refused(_REPO)  # outside the repo, git doesn't use gh


def test_gh_auth_token_timing_out_is_not_read_as_logged_out(github: GitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    github.ready()
    real = subprocess.run

    def run(argv: list[str], **kwargs: object) -> object:
        if argv[:1] == ["gh"]:
            raise subprocess.TimeoutExpired(argv, 15)
        return real(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", run)
    hint = _refused(_REPO)
    assert "didn't answer `gh auth token` in time" in hint and "gh auth login" not in hint


# --- a refusal keeps its own hint wherever it surfaces ----------------------------


@pytest.mark.parametrize(("url", "expected"), [(_REPO, "gh auth login"), ("git@github.com:example-lab/demo.git", SSH_TO_HTTPS)])
def test_an_unreachable_producer_hint_is_the_refusal_when_gh_would_refuse(github: GitHub, url: str, expected: str) -> None:
    """Not "could not reach ...; check your network": git never ran."""
    from mintd.errors import unreachable_repo_hint

    hint = unreachable_repo_hint(url)
    assert expected in hint and "could not reach" not in hint and NETWORK_TOO not in hint


# --- dvc: every call that can clone a producer -----------------------------------


def _import_pointer(path: Path, url: str, *, hashed: bool = False) -> None:
    """An import's .dvc file. ``hashed`` gives its output an md5, which mintd
    needs to read the pointer as an import's (``outs_for_target``)."""
    md5 = f"\n  md5: {'b' * 32}.dir" if hashed else ""
    path.write_text(f"deps:\n- path: data\n  repo:\n    url: {url}\n    rev_lock: {'a' * 40}\nouts:\n- path: {path.stem}{md5}\n")


@pytest.mark.parametrize("verb", ["pull", "status"])
@pytest.mark.parametrize(
    "targets",
    [["x.dvc"], ["x"], None, ["x/inside.csv"], ["local.dvc", "x/inside.csv"]],
    ids=["pointer", "output", "none", "unknown", "known-then-unknown"],
)
def test_dvc_pull_and_status_check_every_github_import_they_may_clone(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, targets: list[str] | None
) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "x.dvc", "git@github.com:example-lab/demo.git")
    _import_pointer(tmp_path / "local.dvc", "/srv/git/demo.git")  # resolvable, and nothing to check
    ops = SubprocessDvcOps(timeouts=Timeouts())
    # Each verb's own error: a refused pull is a failed pull target.
    with pytest.raises(DvcPullRefused if verb == "pull" else DvcImportRepoUnreachable) as raised:
        ops.pull(cwd=tmp_path, targets=targets) if verb == "pull" else ops.status(targets, cwd=tmp_path)
    assert SSH_TO_HTTPS in (raised.value.hint or "")
    assert spawned == []


def test_dvc_pull_of_a_local_import_never_asks_gh(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Spawned(Exception):
        pass

    def spawn(self: SubprocessDvcOps, cmd: list[str], **kwargs: object) -> None:
        raise Spawned

    monkeypatch.setattr(SubprocessDvcOps, "_spawn", spawn)
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "x.dvc", "/srv/git/demo.git")
    with pytest.raises(Spawned):
        SubprocessDvcOps(timeouts=Timeouts()).pull(cwd=tmp_path, targets=["x.dvc"])
    assert github.calls() == []


@pytest.mark.parametrize(
    ("catalog_url", "source"),
    [
        ("https://x-access-token:SECRET@github.com/example-lab/demo.git", _REPO),
        ("https://SECRET@github.com/example-lab/demo", _REPO),
        ("https://user:SECRET@gitlab.example/example-lab/demo", "https://gitlab.example/example-lab/demo"),
        # Taken out of the URL's structure, not by redacting text: a comma or
        # an apostrophe in the credentials doesn't let them through, and
        # another host's URL keeps its own shape.
        ("https://deploy:SECRET,x@gitlab.example/example-lab/demo.git", "https://gitlab.example/example-lab/demo.git"),
        ("https://SECRET'x@gitlab.example/example-lab/demo", "https://gitlab.example/example-lab/demo"),
        ("ssh://git:SECRET@gitlab.example/example-lab/demo.git", "ssh://git@gitlab.example/example-lab/demo.git"),
        ("https://deploy:SECRET@git.example/demo.git", "https://git.example/demo.git"),  # a one-part path
        ("lab:SECRET@lab-server:repos/demo.git", "lab@lab-server:repos/demo.git"),  # scp-like, home-relative
        ("lab@lab-server:repos/demo.git", "lab@lab-server:repos/demo.git"),
        ("lab:p@SECRET@lab-server:repos/demo.git", "lab@lab-server:repos/demo.git"),  # an "@" in the password
        ("git:SECRET@github.com:example-lab/demo.git", _REPO),  # GitHub, so https whatever the credentials
        # The userinfo ends at the last "@" before the first "/": a "#" or "?"
        # in the password doesn't hide it, and git+https drops it all.
        ("https://u:SECRET#x@gitlab.example/example-lab/demo.git", "https://gitlab.example/example-lab/demo.git"),
        ("https://u:SECRET?x@gitlab.example/example-lab/demo.git", "https://gitlab.example/example-lab/demo.git"),
        ("git+https://SECRET@gitlab.example/example-lab/demo.git", "git+https://gitlab.example/example-lab/demo.git"),
        ("https://u:SECRET#x@github.com/example-lab/demo.git", _REPO),
    ],
)
def test_dvc_never_gets_the_catalog_urls_credentials(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_url: str, source: str
) -> None:
    github.ready()
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
    assert cmd[cmd.index("import") + 1] == source
    assert not any("SECRET" in arg for arg in cmd)


def test_dvc_import_checks_exactly_the_url_dvc_gets(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git leaves a URL with credentials alone but rewrites the plain https URL
    dvc is given: the check is on the URL dvc gets."""
    github.ready()
    github.git_config("url.git@github.com:.insteadOf", "https://github.com/")
    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    with pytest.raises(DvcImportRepoUnreachable) as raised:
        SubprocessDvcOps(timeouts=Timeouts()).import_(
            repo_url="https://x-access-token:T@github.com/example-lab/demo.git", path="data", dest=tmp_path / "out", cwd=tmp_path,
        )
    assert SSH_TO_HTTPS in (raised.value.hint or "")
    assert spawned == []


@pytest.mark.parametrize("where", ["root", "subdirectory"])
def test_data_verify_checks_the_projects_imports_from_wherever_it_runs(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    """`data verify` hands dvc its path as given, ".": run as `data verify .`
    from the project and with no target from a folder inside it, the check
    still finds the project's import, and dvc never runs unchecked."""
    project = tmp_path / "proj"
    (project / ".dvc").mkdir(parents=True)
    (project / "code").mkdir()
    (project / "data").mkdir()
    _import_pointer(project / "data" / "demo.dvc", "git@github.com:example-lab/demo.git")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(f"cache_dir: {tmp_path / 'cache'}\n")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(cfg))
    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    monkeypatch.chdir(project if where == "root" else project / "code")
    assert cli.main(["data", "verify", "."] if where == "root" else ["data", "verify"]) == 1
    assert SSH_TO_HTTPS in " ".join(capsys.readouterr().err.split())
    assert spawned == []


def test_a_refused_import_fails_every_dvc_pull_target_with_its_gh_hint(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dvc pull clones every import's producer whatever its targets, so with
    one GitHub import refused, a pipeline output through dvc pull is refused
    too. Each target fails on its own, and says how to fix gh, never "retry
    just this target", which would be refused again."""
    from mintd.data_ops import _pull_isolating_failures

    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "demo.dvc", "git@github.com:example-lab/demo.git")

    class Reporter:
        def __init__(self) -> None:
            self.errors: list[tuple[str, str | None]] = []

        def error(self, msg: str, hint: str | None = None) -> None:
            self.errors.append((msg, hint))

    reporter = Reporter()
    failed = _pull_isolating_failures(
        tmp_path, ["data/stage_out.csv", "demo.dvc"], "storage", {},
        dvc_ops=SubprocessDvcOps(timeouts=Timeouts()), remote=None, jobs=None, extra_dvc_args=None,
        reporter=reporter,  # type: ignore[arg-type]
    )
    assert sorted(failed) == ["data/stage_out.csv", "demo.dvc"]
    assert spawned == []
    assert len(reporter.errors) == 2
    assert all(SSH_TO_HTTPS in (hint or "") and "retry" not in (hint or "") for _, hint in reporter.errors)


def test_dvc_pull_checks_every_import_whatever_its_targets(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    (tmp_path / ".dvc").mkdir()
    (tmp_path / "data").mkdir()
    _import_pointer(tmp_path / "data" / "demo.dvc", "git@github.com:example-lab/demo.git")
    with pytest.raises(DvcPullRefused):  # a pipeline output, no pointer: dvc would still clone the producer
        SubprocessDvcOps(timeouts=Timeouts()).pull(cwd=tmp_path, targets=["model.pkl"])
    assert spawned == []


def test_dvc_status_of_a_pipeline_output_never_asks_gh(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured: `dvc status <output>` clones no producer, so status stays
    scoped to its targets."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: SimpleNamespace(returncode=0, stderr_lines=[], stdout_lines=[]),
    )
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "demo.dvc", "git@github.com:example-lab/demo.git")
    assert SubprocessDvcOps(timeouts=Timeouts()).status(["model.pkl"], cwd=tmp_path) == {}
    assert github.calls() == []



@pytest.mark.parametrize(
    "catalog_url",
    ["https://deploy:SECRET@git.example/demo.git", "deploy:SECRET@git.example:demo.git",
     "git:SECRET@github.com:example-lab/demo.git", "https://x-access-token:SECRET@github.com/example-lab/demo.git"],
)
def test_an_import_made_through_a_credentialed_url_is_still_its_own_producer(tmp_path: Path, catalog_url: str) -> None:
    """The pointer records the URL without its credentials; re-import and bump
    still see one producer, and still refuse another."""
    from mintd._registry_git_ops import without_credentials
    from mintd.data import UnknownProductPath, _require_owner
    from tests._harness.consumer import Import, write_import

    dvc = write_import(
        tmp_path,
        Import(name="demo", producer_url=without_credentials(catalog_url), pin="a" * 40,
               producer_path="data/final", local_path="demo"),
        under="data/imports/demo",
    )
    assert "SECRET" not in dvc.read_text(encoding="utf-8")
    _require_owner(dvc, catalog_url, name="demo", cwd=tmp_path)
    with pytest.raises(UnknownProductPath):
        _require_owner(dvc, "https://git.example/other.git", name="demo", cwd=tmp_path)


def test_a_refused_batch_names_each_target_with_the_gh_fix(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pipeline outputs pull as one batch; refused, each is retried alone,
    and each says how to fix gh."""
    from mintd.data_ops import _pull_isolating_failures

    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: None)
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "demo.dvc", "git@github.com:example-lab/demo.git")

    class Reporter:
        def __init__(self) -> None:
            self.hints: list[str | None] = []

        def error(self, msg: str, hint: str | None = None) -> None:
            self.hints.append(hint)

    reporter = Reporter()
    failed = _pull_isolating_failures(
        tmp_path, ["data/a.csv", "data/b.csv"], "storage", {},
        dvc_ops=SubprocessDvcOps(timeouts=Timeouts()), remote=None, jobs=None, extra_dvc_args=None,
        reporter=reporter,  # type: ignore[arg-type]
    )
    assert sorted(failed) == ["data/a.csv", "data/b.csv"]
    assert len(reporter.hints) == 2 and all(SSH_TO_HTTPS in (h or "") for h in reporter.hints)


def test_an_import_refused_for_another_imports_sake_keeps_the_gh_fix_when_rescue_fails(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dvc pull checks every import, so a healthy import is refused when
    another one isn't set up. If the rescue lane can't land it either, the
    user sees the gh fix, not the rescue's advice."""
    from mintd._import_rescue_ops import RescueResult
    from mintd.data_ops import _pull_targets_with_import_rescue

    github.ready()
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: None)
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "bad.dvc", "git@github.com:example-lab/bad.git", hashed=True)  # SSH: refused
    _import_pointer(tmp_path / "good.dvc", _REPO, hashed=True)  # https, gh ready

    class Reporter:
        def __init__(self) -> None:
            self.errors: list[tuple[str, str | None]] = []

        def error(self, msg: str, hint: str | None = None) -> None:
            self.errors.append((msg, hint))

        def __getattr__(self, name: str) -> Callable[..., None]:
            return lambda *a, **k: None

    reporter = Reporter()
    rescued: list[str] = []

    def rescue(project_path: Path, target: str, **kwargs: object) -> RescueResult:
        rescued.append(target)
        return RescueResult(ok=False, reason="rescue failed", hint="the rescue's advice")

    failed = _pull_targets_with_import_rescue(
        tmp_path, ["good.dvc"], "storage", [],
        dvc_ops=SubprocessDvcOps(timeouts=Timeouts()), remote=None, jobs=None, extra_dvc_args=None,
        reporter=reporter, aws_profile_name=None,  # type: ignore[arg-type]
        import_rescue=rescue,  # type: ignore[arg-type]
    )
    assert rescued == ["good.dvc"]  # it went down the import lane, and the rescue ran
    assert failed == ["good.dvc"]
    [(msg, hint)] = reporter.errors
    assert SSH_TO_HTTPS in (hint or "") and "rescue" not in (hint or "")
    assert "example-lab/bad" in msg and "rescue" not in msg  # names the import that caused the refusal


@pytest.mark.parametrize(("url", "transport"), [("https://gist.github.com/abc123.git", "https"), ("git@gist.github.com:abc123.git", "ssh")])
def test_a_gist_is_on_github(github: GitHub, url: str, transport: str) -> None:
    from mintd._registry_git_ops import github_owner, https_remote, parse_repo_url

    repo = parse_repo_url(url)
    assert repo is not None and (repo.transport, repo.host, repo.repo) == (transport, "gist.github.com", "abc123")
    assert github_owner(url) == "gist.github.com"
    assert https_remote(url) == "https://gist.github.com/abc123"
    assert _refused(url)  # logged out
    github.ready()
    if transport == "ssh":
        assert "change it to https://gist.github.com/abc123" in _refused(url)
    else:  # git matches a helper's host exactly: github.com's doesn't cover gists
        assert "gh auth setup-git" in _refused(url)
        github.git_config("credential.https://gist.github.com.helper", _HELPER)  # as `gh auth setup-git` writes it
        require_github_access(url)


def test_a_nested_dvc_projects_import_is_not_the_outer_projects(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dvc, run from the outer project, never reads a nested project's own
    pointers, so the outer pull isn't refused for them; the nested project's
    own pull is."""
    from types import SimpleNamespace

    spawned: list[list[str]] = []

    def spawn(self: SubprocessDvcOps, cmd: list[str], **kwargs: object) -> object:
        spawned.append(cmd)
        return SimpleNamespace(returncode=0, stderr_lines=[], stdout_lines=[])

    monkeypatch.setattr(SubprocessDvcOps, "_spawn", spawn)
    (tmp_path / ".dvc").mkdir()
    inner = tmp_path / "inner"
    (inner / ".dvc").mkdir(parents=True)
    _import_pointer(inner / "demo.dvc", "git@github.com:example-lab/demo.git")
    ops = SubprocessDvcOps(timeouts=Timeouts())
    ops.pull(cwd=tmp_path, targets=["out.txt"])
    assert len(spawned) == 1 and github.calls() == []
    with pytest.raises(DvcPullRefused):
        ops.pull(cwd=inner, targets=["out.txt"])


def test_each_producer_is_checked_once_per_process(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pull per target asks for every import: the answer is kept."""
    from types import SimpleNamespace

    github.ready()
    monkeypatch.setattr(
        SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: SimpleNamespace(returncode=0, stderr_lines=[], stdout_lines=[]),
    )
    (tmp_path / ".dvc").mkdir()
    for name in ("a", "b", "c"):
        _import_pointer(tmp_path / f"{name}.dvc", f"https://github.com/example-lab/{name}")
    lookups: list[list[str]] = []
    real = subprocess.run

    def run(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "ls-remote", "--get-url"]:
            lookups.append(argv)
        return real(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", run)
    ops = SubprocessDvcOps(timeouts=Timeouts())
    for target in ("x.csv", "y.csv", "z.csv"):
        ops.pull(cwd=tmp_path, targets=[target])
    assert len(lookups) == 3  # one per producer, not one per producer per pull


@pytest.mark.parametrize("target", ["../data/demo.dvc", "../data/demo"])
def test_a_target_above_the_folder_mintd_runs_in_is_checked(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """`data verify ../data/demo.dvc` from a subfolder: the ".." is resolved
    before deciding which project the pointer is in."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    (tmp_path / ".dvc").mkdir()
    (tmp_path / "analysis").mkdir()
    (tmp_path / "data").mkdir()
    _import_pointer(tmp_path / "data" / "demo.dvc", "git@github.com:example-lab/demo.git")
    with pytest.raises(DvcImportRepoUnreachable):
        SubprocessDvcOps(timeouts=Timeouts()).status([target], cwd=tmp_path / "analysis")
    assert spawned == []


def test_no_message_carries_a_password_with_a_hash_in_it(github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The print filter stops a URL's userinfo at "#" or "?"; the refusal and
    dvc import's own errors take credentials out of the URL's structure."""
    from types import SimpleNamespace

    with pytest.raises(GitHubAccessMissing) as raised:  # logged out
        require_github_access("https://u:SECRET#x@github.com/example-lab/demo")
    assert "SECRET" not in f"{raised.value} {raised.value.hint}"

    stderr = ["ERROR: failed to import - SCM error: Failed to clone repo\n"]
    monkeypatch.setattr(
        SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: SimpleNamespace(returncode=1, stderr_lines=stderr, stdout_lines=[]),
    )
    with pytest.raises(DvcImportRepoUnreachable) as failed:
        SubprocessDvcOps(timeouts=Timeouts()).import_(
            repo_url="https://u:SECRET#x@gitlab.example/example-lab/demo.git", path="data", dest=tmp_path / "out", cwd=tmp_path,
        )
    assert "SECRET" not in f"{failed.value} {failed.value.hint}"


_DEMO = "git@github.com:example-lab/demo.git"
_NESTED = "git@github.com:example-lab/nested.git"


@pytest.mark.parametrize(
    ("run_in", "cwd", "targets", "found"),
    [
        ("proj/analysis", ".", ["../data/demo.dvc"], [_DEMO]),
        ("proj/analysis", ".", ["../data/demo"], [_DEMO]),
        ("sib", "../proj", ["data/demo"], [_DEMO]),  # `data verify --path ../proj data/demo`
        ("sib", "../proj", ["data/demo/inside.csv"], [_DEMO]),
        ("proj", ".", ["."], [_DEMO]),  # `data verify .`; the nested project's import isn't this one's
        ("proj", ".", None, [_DEMO]),
        ("proj/analysis", ".", None, [_DEMO]),
        ("proj/inner", ".", None, [_NESTED]),  # inside the nested project, its own import
        ("proj", ".", ["data\\demo.dvc"], [_DEMO]),  # a backslash, a separator to dvc
        ("proj", ".", ["data\\demo"], [_DEMO]),
        # Symlinked paths passed as the working folder itself, as an absolute
        # `--path` through a symlink would be, not through a chdir (which
        # resolves the link before mintd sees it).
        ("sib", "{tmp}/link/analysis", ["../data/demo"], [_DEMO]),  # link -> the project
        ("sib", "{tmp}/alink", None, [_DEMO]),  # alink -> the project's analysis folder
        ("sib", "{tmp}/alink", ["../data/demo"], [_DEMO]),
    ],
)
def test_every_path_is_compared_in_one_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_in: str, cwd: str, targets: list[str] | None, found: list[str]
) -> None:
    from mintd._dvc_ops import _import_urls

    project = tmp_path / "proj"
    for folder in (project / ".dvc", project / "analysis", project / "data" / "demo", project / "inner" / ".dvc", tmp_path / "sib"):
        folder.mkdir(parents=True)
    _import_pointer(project / "data" / "demo.dvc", _DEMO)
    _import_pointer(project / "inner" / "nested.dvc", _NESTED)
    (tmp_path / "link").symlink_to(project)
    (tmp_path / "alink").symlink_to(project / "analysis")
    monkeypatch.chdir(tmp_path / run_in)
    assert _import_urls(Path(cwd.format(tmp=tmp_path)), targets) == found


_WEIRD = "git@github.com:example-lab/weird.git"


@pytest.mark.skipif(os.name == "nt", reason="on Windows a backslash can't be part of a folder name")
@pytest.mark.parametrize("folder", ["proj", "pro\\j"])
@pytest.mark.parametrize(
    ("targets", "found"), [(None, [_DEMO, _WEIRD]), (["data/demo"], [_DEMO]), (["."], [_DEMO, _WEIRD])]
)
def test_a_backslash_in_a_folder_or_pointer_name_stays_part_of_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str, targets: list[str] | None, found: list[str]
) -> None:
    """dvc reads a backslash as a separator in a target only: a project in a
    folder named pro\\j, and a pointer named we\\ird.dvc, are still found."""
    from mintd._dvc_ops import _import_urls

    project = tmp_path / folder
    for sub in (project / ".dvc", project / "data" / "demo"):
        sub.mkdir(parents=True)
    _import_pointer(project / "data" / "demo.dvc", _DEMO)
    _import_pointer(project / "we\\ird.dvc", _WEIRD)
    monkeypatch.chdir(project)
    assert sorted(_import_urls(Path("."), targets)) == found


def test_www_github_passes_on_github_coms_helper(github: GitHub) -> None:
    """GitHub answers www.github.com with a redirect to github.com, and git
    then asks github.com's credential helper, as `gh auth setup-git` writes it."""
    github.ready()
    require_github_access("https://www.github.com/example-lab/demo")
    assert "gh auth setup-git" not in github_access_hint("https://www.github.com/example-lab/demo")


def test_dvc_imports_missing_path_error_names_the_url_dvc_was_given(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from mintd._dvc_ops import DvcImportPathNotFound

    stderr = ["ERROR: failed to import 'data' - 'data' Does not exist\n"]
    monkeypatch.setattr(
        SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: SimpleNamespace(returncode=1, stderr_lines=stderr, stdout_lines=[]),
    )
    with pytest.raises(DvcImportPathNotFound) as raised:
        SubprocessDvcOps(timeouts=Timeouts()).import_(
            repo_url="https://u:SECRET#x@gitlab.example/example-lab/demo.git", path="data", dest=tmp_path / "out", cwd=tmp_path,
        )
    assert "https://gitlab.example/example-lab/demo.git" in str(raised.value)
    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize(
    "catalog_url",
    ["https://u:TOK#E?N@X@gitlab.example/example-lab/demo.git", "https://u:TOK#E?N@X@github.com/example-lab/demo"],
)
def test_the_unreachable_hint_never_prints_a_password(github: GitHub, catalog_url: str) -> None:
    """The hint names the URL as a value, so its credentials come out of its
    structure: a "#", "?" or "@" in the password doesn't let it through."""
    from mintd.errors import unreachable_repo_hint

    github.ready()
    hint = unreachable_repo_hint(catalog_url)
    assert "could not reach" in hint
    assert "TOK" not in hint and "N@X" not in hint


# --- GitHub access together with a pointer hash dvc never writes ------------

_BAD_PRODUCER_POINTER = (
    "outs:\n- path: data\n  hash: md5\n  files:\n  - relpath: a.csv\n    md5: ab/../../a.csv\n    size: 1\n"
)
_PRODUCER_CONFIG = "[core]\n    remote = storage\n['remote \"storage\"']\n    url = s3://example-lab-bucket/demo\n"


def _producer_over_git_archive(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> list[str]:
    """The producer's files as `git archive --remote` would serve them, for the
    real GitArchiveFetcher; the GitHub check's own gh and git calls run for real.
    Returns the paths archived, in order."""
    import io
    import tarfile

    from tests._harness.access import REAL_RUN, is_access_check

    archived: list[str] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if is_access_check(argv):
            return REAL_RUN(argv, **kwargs)
        assert "archive" in argv, argv
        path = argv[-1]
        archived.append(path)
        if path not in files:
            return subprocess.CompletedProcess(argv, 128, b"", f"fatal: pathspec '{path}' did not match any files\n".encode())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=path)
            info.size = len(files[path].encode())
            tar.addfile(info, io.BytesIO(files[path].encode()))
        return subprocess.CompletedProcess(argv, 0, buf.getvalue(), b"")

    monkeypatch.setattr("mintd._producer_git_ops.subprocess.run", run)
    return archived


@pytest.mark.parametrize("gh_ready", [False, True])
def test_a_github_import_whose_producer_pointer_has_a_bad_hash(
    github: GitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh_ready: bool
) -> None:
    """dvc pull can't land the import, so the rescue reads the producer's pointer,
    and that pointer records a hash dvc never writes.

    gh not ready: dvc pull is refused before it runs, and so is the rescue's first
    fetch from the producer; the pointer is never read, and the user gets the gh fix.
    That is right: nothing of the producer can be read until gh is set up, so it's the
    first thing to fix, and the hash refusal follows once it is.
    gh ready: the rescue fetches the producer's config, then its pointer, and refuses
    the hash, naming the producer's file, its repo and pin, as #117's rescue test pins
    for a producer that never reached the check.

    Mutations: drop the gh check in the fetcher's `_run` -> the not-ready case reads
    the pointer and raises, red; drop the hash check in `_entries_from_producer_pointer`
    -> the ready case reaches S3 and returns a rescue failure instead, red."""
    from mintd._fast_sync_ops import PointerCutShort
    from mintd._import_rescue_ops import rescue_import_pull
    from mintd.data_ops import _pull_targets_with_import_rescue
    from types import SimpleNamespace

    if gh_ready:
        github.ready()
    monkeypatch.setattr(  # dvc pull runs only when gh is ready, and can't land the import
        SubprocessDvcOps, "_spawn",
        lambda self, cmd, **kw: SimpleNamespace(returncode=1, stderr_lines=["ERROR: failed to pull data\n"], stdout_lines=[]),
    )
    (tmp_path / ".dvc").mkdir()
    _import_pointer(tmp_path / "demo.dvc", _REPO, hashed=True)
    archived = _producer_over_git_archive(monkeypatch, {".dvc/config": _PRODUCER_CONFIG, "data.dvc": _BAD_PRODUCER_POINTER})

    class Reporter:
        def __init__(self) -> None:
            self.errors: list[tuple[str, str | None]] = []

        def error(self, msg: str, hint: str | None = None) -> None:
            self.errors.append((msg, hint))

        def __getattr__(self, name: str) -> Callable[..., None]:
            return lambda *a, **k: None

    reporter = Reporter()

    def pull() -> list[str]:
        return _pull_targets_with_import_rescue(
            tmp_path, ["demo.dvc"], "storage", [],
            dvc_ops=SubprocessDvcOps(timeouts=Timeouts()), remote=None, jobs=None, extra_dvc_args=None,
            reporter=reporter, aws_profile_name=None,  # type: ignore[arg-type]
            import_rescue=rescue_import_pull,
        )

    if not gh_ready:
        assert pull() == ["demo.dvc"]
        assert archived == []  # nothing of the producer was read
        ((msg, hint),) = reporter.errors
        assert "gh auth login" in (hint or ""), (msg, hint)
        assert "shape dvc writes" not in msg + (hint or "")
    else:
        with pytest.raises(PointerCutShort) as refused:
            pull()
        assert str(refused.value) == (
            f"data.dvc in {_REPO} at aaaaaaa is cut short or damaged: "
            "a hash in it is not in the shape dvc writes: 'ab/../../a.csv'"
        )
        assert archived == [".dvc/config", "data.dvc"]


@pytest.mark.parametrize("gh_ready", [False, True])
def test_a_pull_with_a_github_import_and_a_bad_hash_pointer_names_the_pointer(
    github: GitHub, s3_versioned, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, gh_ready: bool
) -> None:
    """A project's own pointer records a hash dvc never writes, beside a GitHub
    import. `mintd data pull` stops on the pointer, naming it, before any dvc pull,
    so the GitHub check never runs, ready or not.

    That is right: the pull changes nothing (a damaged pointer could name a path
    outside the cache, which #117 stops), the message names the file to restore,
    and with that fixed the next pull reaches the import and its GitHub check.
    It is also why the gh fix isn't shown here: no clone was about to happen.

    Mutation: drop the hash check in `_check_out` -> the pull goes on to dvc pull
    (and, gh not ready, to the gh refusal), red."""
    from unittest.mock import patch

    s3, bucket = s3_versioned
    if gh_ready:
        github.ready()
    spawned: list[list[str]] = []
    monkeypatch.setattr(SubprocessDvcOps, "_spawn", lambda self, cmd, **kw: spawned.append(cmd))
    project = tmp_path / "project"
    (project / "data" / "imports").mkdir(parents=True)
    (project / ".dvc").mkdir()
    (project / ".dvc" / "config").write_text(f"[core]\n    remote = origin\n['remote \"origin\"']\n    url = s3://{bucket}\n")
    (project / "data" / "dir.dvc").write_text("outs:\n  - path: dir\n    md5: ab/home/u/notes.dir\n")
    _import_pointer(project / "data" / "imports" / "demo.dvc", _REPO, hashed=True)
    with patch("mintd._fast_sync_ops._create_s3_client", return_value=s3):
        rc = cli.main(["data", "pull", "--path", str(project)])
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 1, err
    assert "dir.dvc is cut short or damaged: the hash of dir is not in the shape dvc writes" in err, err
    assert "gh auth" not in err
    assert spawned == []  # no dvc pull ran
    assert github.calls() == []  # and no GitHub check
