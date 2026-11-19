"""`.dvc/config` and `.dvc/config.local`, read the way dvc reads them: `_dvc_state`'s one
reader, compared case by case with real dvc's `dvc config` on the same files."""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd._dvc_state import dvc_cache_dir, get_remote_config, merged_remote
from mintd.data_ops import _default_dvc_remote, _local_redirect
from mintd.errors import MintdError

R = "['remote \"storage\"']\n"


def _project(real_dvc, root: Path, config: str, local: str | None = None) -> Path:
    real_dvc(["init", "-q", "--no-scm"], cwd=root, check=True)
    (root / ".dvc" / "config").write_text(config)
    if local is not None:
        (root / ".dvc" / "config.local").write_text(local)
    return root


def _dvc_reads(real_dvc, root: Path, key: str, *level: str) -> str:
    return real_dvc(["config", *level, key], cwd=root, check=True).stdout.strip()


@pytest.mark.parametrize(("config", "local", "key", "want"), [
    pytest.param(f'{R}    url = "s3://b/p"\n', None, "url", "s3://b/p", id="a-quoted-value"),
    pytest.param(f"{R}    url = s3://b/p  # the lab bucket\n", None, "url", "s3://b/p", id="an-inline-comment"),
    pytest.param(f"{R}    url = s3://b/p ; kept\n", None, "url", "s3://b/p ; kept", id="a-semicolon-is-no-comment"),
    pytest.param(f"{R}    url = s3://b/50%\n", None, "url", "s3://b/50%", id="a-lone-percent"),
    pytest.param(f"{R}    URL = s3://b/p\n", None, "url", "s3://b/p", id="an-uppercase-key"),
    pytest.param('[remote "storage"]\n    url = s3://b/p\n', None, "url", "s3://b/p", id="a-double-quoted-header"),
    pytest.param(f"{R}    url = s3://b/p\n    endpointurl =\n", None, "endpointurl", "", id="an-empty-value"),
    pytest.param(f"{R}    url = s3://b/p\n", f"{R}    endpointurl = http://localhost:9\n", "endpointurl",
                 "http://localhost:9", id="config-local-adds-a-key"),
    pytest.param(f"{R}    url = s3://b/p\n", f"{R}    url = s3://scratch/x\n", "url", "s3://scratch/x",
                 id="config-local-overrides-a-key"),
])
def test_the_push_view_reads_each_value_as_dvc_reads_it(real_dvc, tmp_path: Path, config, local, key, want) -> None:
    """`merged_remote` is the remote as dvc pushes with it, so each value it reads is the one
    `dvc config` reads from the same files: quotes and inline `#` comments stripped, `;` and a
    lone `%` kept, keys case-insensitive, `config.local` laid over `.dvc/config`.
    Mutation that must redden this: read with configparser again."""
    root = _project(real_dvc, tmp_path, config, local)
    assert _dvc_reads(real_dvc, root, f"remote.storage.{key}") == want
    assert merged_remote(root, "storage").get(key) == want


@pytest.mark.parametrize(("config", "local"), [
    pytest.param(f"[core]\n    remote = storage\n{R}    url = s3://b/p\n", None, id="core-remote-in-config"),
    pytest.param(f"{R}    url = s3://b/p\n['remote \"other\"']\n    url = s3://o/p\n", "[core]\n    remote = storage\n",
                 id="core-remote-only-in-config-local"),
    pytest.param(f"[core]\n    remote = other\n{R}    url = s3://b/p\n['remote \"other\"']\n    url = s3://o/p\n",
                 "[core]\n    remote = storage\n", id="config-local-over-config"),
    pytest.param("[core]\n    remote = MyRemote\n['remote \"MyRemote\"']\n    url = s3://b/p\n", None,
                 id="a-mixed-case-remote-name"),
    pytest.param("", f"[core]\n    remote = storage\n{R}    url = s3://b/p\n", id="a-remote-only-in-config-local"),
])
def test_the_default_remote_and_its_url_are_the_ones_dvc_uses(real_dvc, tmp_path: Path, config, local) -> None:
    """The default remote is `core.remote` from `.dvc/config.local` over `.dvc/config`, matched
    to its section without regard to case, as dvc's schema matches it; fast sync and the cache
    lane then read the url dvc pushes to, for a remote defined only in `config.local` too.
    Mutations that must redden this: read core.remote from .dvc/config alone; stop lowercasing
    core.remote; drop the config.local section of a remote with no committed one."""
    root = _project(real_dvc, tmp_path, config, local)
    name = _default_dvc_remote(root)
    assert name == _dvc_reads(real_dvc, root, "core.remote").lower()
    assert get_remote_config(root, name)["url"] == _dvc_reads(real_dvc, root, f"remote.{name}.url")


@pytest.mark.parametrize(("config", "local", "cache"), [
    pytest.param("[cache]\n    dir = ../committed\n", None, "committed", id="in-config"),
    pytest.param("[cache]\n    dir = ../committed\n", "[cache]\n    dir = ../mine\n", "mine", id="config-local-over-config"),
])
def test_the_cache_dir_is_the_one_dvc_uses(real_dvc, tmp_path: Path, config, local, cache) -> None:
    """Push's pointer checks read the cache dvc writes to: `cache.dir` from `config.local` over
    `.dvc/config`, relative to `.dvc/`. Mutation that must redden this: read .dvc/config first."""
    root = _project(real_dvc, tmp_path, config, local)
    assert Path(_dvc_reads(real_dvc, root, "cache.dir")).resolve() == dvc_cache_dir(root).resolve()
    assert dvc_cache_dir(root).resolve() == (root / cache).resolve()


@pytest.mark.parametrize(("value", "dvc_raw", "version_aware"), [
    pytest.param('"true"', "true", True, id="quoted"),
    pytest.param("True", "True", True, id="capitalised"),
    pytest.param("true ; note", "true ; note", False, id="a-semicolon-is-no-comment"),
])
def test_version_aware_reads_as_dvc_reads_it(real_dvc, tmp_path: Path, value, dvc_raw, version_aware) -> None:
    """`version_aware` decides whether a consumer's pull needs an S3 version ID per file. dvc
    strips the quotes and keeps a `;`, and its schema refuses `true ; note`, so the storage
    check reads the md5 layout there, never a traceback.
    Mutation that must redden this: strip `;` comments again in `_storage_state`."""
    from mintd._storage_state import _read_dvc_config

    root = _project(real_dvc, tmp_path, f"[core]\n    remote = storage\n{R}    url = s3://b/p\n    version_aware = {value}\n")
    assert _dvc_reads(real_dvc, root, "remote.storage.version_aware", "--project") == dvc_raw
    assert _read_dvc_config(root / ".dvc" / "config") == ("storage", "s3://b/p", version_aware)


# --- what changes on purpose: dvc can't read these files, so mintd stops too ----------------

_READERS = {
    "the-push-check": lambda root: merged_remote(root, "storage"),
    "publishs-redirect-check": lambda root: _local_redirect(root, "storage", named=False),
    "the-default-remote": _default_dvc_remote,
    "the-cache-dir": dvc_cache_dir,
}


@pytest.mark.parametrize("broken", [
    pytest.param("[core\n", id="an-unclosed-header"),
    pytest.param(f"{R}    url = s3://b/p\n    url = s3://c/p\n", id="a-duplicate-key"),
])
@pytest.mark.parametrize(("level", "reader"), [
    ("config.local", "the-push-check"), ("config", "the-push-check"),
    ("config.local", "publishs-redirect-check"),
    ("config.local", "the-default-remote"), ("config", "the-default-remote"),
    ("config.local", "the-cache-dir"), ("config", "the-cache-dir"),
])
def test_a_config_dvc_cannot_read_stops_mintd_too(real_dvc, tmp_path: Path, broken, level, reader) -> None:
    """dvc stops on a config file it can't parse, so each of mintd's readers stops too, naming
    the file, instead of reading past it as before: the push check skipped an unreadable
    `config.local`, publish's redirect check called it "no redirect", the default remote was
    "none" (so `data pull` and `data push` guessed `origin`), the cache dir fell back to
    `.dvc/cache`, and configparser read past a duplicate key.
    Mutation that must redden this: catch the reader's error and carry on in any of them."""
    root = _project(real_dvc, tmp_path, f"[core]\n    remote = storage\n{R}    url = s3://b/p\n")
    (root / ".dvc" / level).write_text(broken)
    assert real_dvc(["config", "--list"], cwd=root).returncode != 0
    with pytest.raises(MintdError, match=f"can't read .*{level}"):
        _READERS[reader](root)


def test_two_remotes_and_no_default_give_the_storage_check_no_remote(tmp_path: Path) -> None:
    """dvc picks no remote from two with no `core.remote`; the storage check no longer takes
    the first section. Mutation that must redden this: take the first remote section again."""
    from mintd._storage_state import _read_dvc_config

    (tmp_path / "config").write_text(f"{R}    url = s3://b/p\n['remote \"other\"']\n    url = s3://o/p\n")
    assert _read_dvc_config(tmp_path / "config") == (None, None, False)


def test_a_remote_only_in_config_local_is_read_by_fast_sync(real_dvc, tmp_path: Path) -> None:
    """A pre-mintd repo can keep its remote in someone's `config.local` and nothing in
    `.dvc/config`. dvc pushes there, so fast sync and the cache lane read it too, instead of
    "remote not found". Mutation that must redden this: drop the config.local section."""
    root = _project(real_dvc, tmp_path, "", f"[core]\n    remote = storage\n{R}    url = s3://b/p\n    profile = lab\n")
    assert get_remote_config(root, "storage") == {"url": "s3://b/p", "profile": "lab"}
    assert _dvc_reads(real_dvc, root, "remote.storage.url") == "s3://b/p"


# --- commit D: the two configs dvc refuses, refused by mintd too -----------------------------


def test_an_unquoted_remote_header_stops_as_it_stops_dvc(real_dvc, tmp_path: Path) -> None:
    """dvc's schema refuses `[remote origin]` without quotes ("extra keys not allowed"), so
    every dvc command fails on that config. mintd stops too, naming the header and the fix,
    where it used to read the section as the remote.
    Mutation that must redden this: accept the unquoted header again."""
    root = _project(real_dvc, tmp_path, "[core]\n    remote = origin\n[remote origin]\n    url = s3://b/p\n")
    dvc = real_dvc(["status"], cwd=root)
    assert dvc.returncode != 0 and "extra keys not allowed" in dvc.stdout + dvc.stderr
    with pytest.raises(MintdError, match=r"an unquoted \[remote origin\] header") as raised:
        get_remote_config(root, "origin")
    assert raised.value.hint == 'write [remote "origin"]; dvc can\'t read this config either'


def _consumer_of_one_import(real_dvc, tmp_path: Path) -> Path:
    """A consumer with one real `dvc import` and no remote of its own, its data not yet pulled."""
    import shutil

    from tests._harness.git import _git
    from tests._harness.producer import build_local_producer

    producer = build_local_producer(tmp_path / "prod")
    producer.publish({"data/final": {"a.csv": b"v1\n"}})
    consumer = tmp_path / "consumer"
    (consumer / "data").mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    real_dvc(["import", producer.url, "data/final", "-o", "data/final"], cwd=consumer, check=True)
    shutil.rmtree(consumer / "data" / "final")
    shutil.rmtree(consumer / ".dvc" / "cache", ignore_errors=True)
    return consumer


def test_imports_only_and_no_remote_pull_as_dvc_pulls(real_dvc, tmp_path: Path, monkeypatch, capsys) -> None:
    """Imports fetch from their producers, so a consumer of imports alone needs no remote of
    its own: `dvc pull` lands them, and so does `mintd data pull`, which asks for a default
    remote only once a target needs the project's own.
    Mutation that must redden this: require the default before looking at the targets."""
    import shutil

    from mintd import cli

    consumer = _consumer_of_one_import(real_dvc, tmp_path)
    real_dvc(["pull"], cwd=consumer, check=True)
    assert (consumer / "data/final/a.csv").read_bytes() == b"v1\n"
    shutil.rmtree(consumer / "data" / "final")
    shutil.rmtree(consumer / ".dvc" / "cache", ignore_errors=True)
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    assert cli.main(["data", "pull", "--path", str(consumer)]) == 0, capsys.readouterr().err
    assert (consumer / "data/final/a.csv").read_bytes() == b"v1\n"


def test_one_remote_and_no_default_is_refused_as_dvc_refuses_it(real_dvc, tmp_path: Path, monkeypatch, capsys) -> None:
    """With one remote and no `core.remote`, dvc picks none: `dvc pull` stops with "No remote
    provided and no default remote set". `mintd data pull` stops too, before any download,
    with a hint naming that remote, and never runs dvc (whose raw message the user would see).
    It used to take the one remote.
    Mutations that must redden this: take the one remote again; fall back to dvc pull."""
    import shutil

    from mintd import _dvc_ops, cli

    project = tmp_path / "project"
    project.mkdir()
    real_dvc(["init", "-q", "--no-scm"], cwd=project, check=True)
    real_dvc(["remote", "add", "store", str(tmp_path / "store")], cwd=project, check=True)
    (project / "x.csv").write_text("x\n")
    real_dvc(["add", "-q", "x.csv"], cwd=project, check=True)
    real_dvc(["push", "-r", "store"], cwd=project, check=True)
    (project / "x.csv").unlink()
    shutil.rmtree(project / ".dvc" / "cache")
    dvc = real_dvc(["pull"], cwd=project)
    assert dvc.returncode != 0 and "no default remote" in (dvc.stdout + dvc.stderr).lower()

    def no_dvc(*_a, **_k):
        raise AssertionError("mintd ran dvc pull")

    monkeypatch.setattr(_dvc_ops.SubprocessDvcOps, "pull", no_dvc)
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    assert cli.main(["data", "pull", "--path", str(project)]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "error: no default DVC remote in .dvc/config" in err
    assert "hint: set a default: dvc remote default store (add --local to keep it out of git)" in err
