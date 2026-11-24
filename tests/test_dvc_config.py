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
    pytest.param("[cache]\n    dir = ../committed\n", "[cache]\n    dir =\n", ".dvc", id="an-empty-dir-in-config-local"),
    pytest.param("[cache]\n    dir = ~/home-cache\n", None, "dvc-home/home-cache", id="a-dir-under-home"),
])
def test_the_cache_dir_is_the_one_dvc_uses(real_dvc, tmp_path: Path, monkeypatch, config, local, cache) -> None:
    """Push's pointer checks read the cache dvc writes to: `cache.dir` from `config.local` over
    `.dvc/config`, `~` expanded, relative to `.dvc/`; an empty `dir =` overrides too, and dvc
    then writes into `.dvc/` itself. Mutations that must redden this: read .dvc/config first;
    skip an empty value; leave `~` unexpanded."""
    for var in ("HOME", "USERPROFILE"):  # the home real_dvc gives dvc
        monkeypatch.setenv(var, str(tmp_path / "dvc-home"))
    root = _project(real_dvc, tmp_path, config, local)
    dvc_dir = real_dvc(["cache", "dir"], cwd=root, check=True).stdout.strip()
    assert Path(dvc_dir).resolve() == dvc_cache_dir(root).resolve()
    assert dvc_cache_dir(root).resolve() == (tmp_path / cache).resolve()


@pytest.mark.parametrize(("value", "dvc_raw", "version_aware"), [
    pytest.param('"true"', "true", True, id="quoted"),
    pytest.param("True", "True", True, id="capitalised"),
    pytest.param("true ; note", "true ; note", False, id="a-semicolon-is-no-comment"),
])
def test_version_aware_reads_as_dvc_reads_it(real_dvc, tmp_path: Path, value, dvc_raw, version_aware) -> None:
    """`version_aware` decides whether a consumer's pull needs an S3 version ID per file. dvc
    strips the quotes and keeps a `;`, and its schema refuses `true ; note`, so the storage
    check never reads it as version-aware, and says dvc refuses it, never a traceback.
    Mutation that must redden this: strip `;` comments again in `_storage_state`."""
    from mintd._storage_state import _read_dvc_config

    root = _project(real_dvc, tmp_path, f"[core]\n    remote = storage\n{R}    url = s3://b/p\n    version_aware = {value}\n")
    assert _dvc_reads(real_dvc, root, "remote.storage.version_aware", "--project") == dvc_raw
    name, url, aware, refused = _read_dvc_config(root / ".dvc" / "config")
    assert (name, url, aware) == ("storage", "s3://b/p", version_aware)
    assert (refused is not None) == (value == "true ; note")


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
    assert _read_dvc_config(tmp_path / "config") == (None, None, False, None)


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


def _consumer_of_one_import(real_dvc, tmp_path: Path, *flags: str) -> Path:
    """A consumer with one real `dvc import` and no remote of its own, its data not yet pulled.
    ``flags`` go to `dvc import`: `--no-download` records the import with no hash."""
    import shutil

    from tests._harness.git import _git
    from tests._harness.producer import build_local_producer

    producer = build_local_producer(tmp_path / "prod")
    producer.publish({"data/final": {"a.csv": b"v1\n"}})
    consumer = tmp_path / "consumer"
    (consumer / "data").mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    real_dvc(["import", *flags, producer.url, "data/final", "-o", "data/final"], cwd=consumer, check=True)
    shutil.rmtree(consumer / "data" / "final", ignore_errors=True)
    shutil.rmtree(consumer / ".dvc" / "cache", ignore_errors=True)
    return consumer


@pytest.mark.parametrize("flags", [pytest.param((), id="imported"), pytest.param(("--no-download",), id="no-download")])
def test_imports_only_and_no_remote_pull_as_dvc_pulls(real_dvc, tmp_path: Path, monkeypatch, capsys, flags) -> None:
    """Imports fetch from their producers, so a consumer of imports alone needs no remote of
    its own: `dvc pull` lands them, and so does `mintd data pull`, which asks for a default
    remote only once a target needs the project's own. An import made with `--no-download`
    (`mintd data import` forwards it) records no hash; it lands too.
    Mutations that must redden this: require the default before looking at the targets; ask
    for one when a target reaches no out with a hash."""
    import shutil

    from mintd import cli

    consumer = _consumer_of_one_import(real_dvc, tmp_path, *flags)
    pointer = (consumer / "data" / "final.dvc").read_text()
    real_dvc(["pull"], cwd=consumer, check=True)
    (consumer / "data" / "final.dvc").write_text(pointer)  # as the import left it
    assert (consumer / "data/final/a.csv").read_bytes() == b"v1\n"
    shutil.rmtree(consumer / "data" / "final")
    shutil.rmtree(consumer / ".dvc" / "cache", ignore_errors=True)
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    assert cli.main(["data", "pull", "--path", str(consumer)]) == 0, capsys.readouterr().err
    assert (consumer / "data/final/a.csv").read_bytes() == b"v1\n"


def _drop_cache(root: Path) -> None:
    """Remove the project's dvc cache. dvc writes its files read-only, which Windows won't delete."""
    import shutil

    cache = root / ".dvc" / "cache"
    for path in cache.rglob("*"):
        path.chmod(0o700)
    shutil.rmtree(cache)


_STAGE = [["stage", "add", "-n", "make", "-o", "x.csv", "echo x > x.csv"], ["repro", "-q"]]


@pytest.mark.parametrize(("track", "targets"), [
    pytest.param([["add", "-q", "x.csv"]], [], id="a-pointer"),
    pytest.param(_STAGE, [], id="a-pipeline-out"),
    pytest.param(_STAGE, ["x.csv"], id="a-pipeline-out-by-name"),
])
def test_one_remote_and_no_default_is_refused_as_dvc_refuses_it(
    real_dvc, tmp_path: Path, monkeypatch, capsys, track, targets,
) -> None:
    """With one remote and no `core.remote`, dvc picks none: `dvc pull` stops with "No remote
    provided and no default remote set". `mintd data pull` stops too, before any download,
    with a hint naming that remote, and never runs dvc (whose raw message the user would see).
    It used to take the one remote.
    Mutations that must redden this: take the one remote again; fall back to dvc pull; for a
    pipeline out, ask only about the outs fast sync can serve (with no remote, none); pass a
    named target that reads as no out (a stage out by its path)."""

    from mintd import _dvc_ops, cli

    project = tmp_path / "project"
    project.mkdir()
    real_dvc(["init", "-q", "--no-scm"], cwd=project, check=True)
    real_dvc(["remote", "add", "store", str(tmp_path / "store")], cwd=project, check=True)
    (project / "x.csv").write_text("x\n")
    for argv in track:
        real_dvc(argv, cwd=project, check=True)
    real_dvc(["push", "-r", "store"], cwd=project, check=True)
    (project / "x.csv").unlink()
    _drop_cache(project)
    dvc = real_dvc(["pull"], cwd=project)
    assert dvc.returncode != 0 and "no default remote" in (dvc.stdout + dvc.stderr).lower()

    def no_dvc(*_a, **_k):
        raise AssertionError("mintd ran dvc pull")

    monkeypatch.setattr(_dvc_ops.SubprocessDvcOps, "pull", no_dvc)
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    monkeypatch.chdir(project)
    assert cli.main(["data", "pull", "--path", str(project), *targets]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "error: no default DVC remote in .dvc/config" in err
    assert "hint: set a default: dvc remote default store (add --local to keep it out of git)" in err


@pytest.mark.parametrize(("track", "targets"), [
    pytest.param([["add", "-q", "x.csv"]], [], id="a-pointer"),
    pytest.param(_STAGE, [], id="a-pipeline-out"),
    pytest.param(_STAGE, ["x.csv"], id="a-pipeline-out-by-name"),
])
def test_no_default_remote_and_the_data_in_the_cache_pull_as_dvc_pulls(
    real_dvc, tmp_path: Path, monkeypatch, capsys, track, targets,
) -> None:
    """With no remote to pull from, `dvc pull` still restores what its cache holds (with a
    warning), so a project needs a default remote only for data the cache lacks; mintd asks
    for one only then, for a stage out named by its path too. Mutations that must redden this:
    ask whatever the cache holds; ask for any named target that has no pointer."""
    from mintd import cli

    project = tmp_path / "project"
    project.mkdir()
    real_dvc(["init", "-q", "--no-scm"], cwd=project, check=True)
    (project / "x.csv").write_text("x\n")
    for argv in track:
        real_dvc(argv, cwd=project, check=True)
    tracked = (project / "x.csv").read_bytes()  # what the stage wrote: Windows' `echo` adds a space
    (project / "x.csv").unlink()
    real_dvc(["pull"], cwd=project, check=True)
    assert (project / "x.csv").read_bytes() == tracked
    (project / "x.csv").unlink()
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    monkeypatch.chdir(project)
    assert cli.main(["data", "pull", "--path", str(project), *targets]) == 0, capsys.readouterr().err
    assert (project / "x.csv").read_bytes() == tracked


def test_clone_matches_a_path_with_no_default_remote_and_leaves_the_refusal_to_pull(tmp_path: Path) -> None:
    """Clone matches a `--path` to the clone's outs by path, which needs no remote; data pull
    then refuses only where the project's own remote is needed, so imports alone still clone.
    Mutation that must redden this: require the default remote in the resolver again."""
    from mintd.data import _resolve_targets_in_clone

    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f"{R}    url = s3://b/p\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv.dvc").write_text("outs:\n- md5: 0123456789abcdef0123456789abcdef\n  path: x.csv\n")
    targets, _ = _resolve_targets_in_clone(tmp_path, targets=["data/x.csv"], name="p", rev_primary=False)
    assert targets == ["data/x.csv"]


@pytest.mark.parametrize(("config", "hint"), [
    pytest.param(f"{R}    url = s3://b/lab/p\n",
                 ".dvc/config has remote 'storage' but sets no default remote. Run: dvc remote default storage",
                 id="one-remote-and-no-default"),
    pytest.param("[remote storage]\n    url = s3://b/lab/p\n",
                 'an unquoted [remote storage] header; write [remote "storage"]', id="an-unquoted-header"),
])
def test_check_names_the_fix_for_a_config_with_no_default_remote(tmp_path: Path, config: str, hint: str) -> None:
    """`mintd check` reads no default remote from these, as dvc does; its repair is the one dvc
    takes, not `dvc remote add`, which fails on both. Mutation that must redden this: drop the
    two cases from the repair hint."""
    import json

    from mintd._storage_state import StorageState, inspect_storage, repair_hint

    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(config)
    storage = {"bucket": "b", "prefix": "lab/p", "dvc": {"remote_name": "storage"}}
    (tmp_path / "metadata.json").write_text(json.dumps({"storage": storage}))
    inspection = inspect_storage(tmp_path)
    assert inspection.state == StorageState.PARTIAL_META_ONLY
    assert hint in (repair_hint(inspection) or "")


def test_a_target_that_is_no_output_gets_dvcs_answer_not_the_remote_hint(
    real_dvc, tmp_path: Path, monkeypatch, capsys,
) -> None:
    """With no default remote, a target that reaches no tracked out is a typo, not a call for a
    remote: dvc says it "does not exist as an output", and mintd passes that on rather than
    asking for a default remote. Mutation that must redden this: ask for one when a target
    reaches no out."""
    from mintd import cli

    consumer = _consumer_of_one_import(real_dvc, tmp_path)
    dvc = real_dvc(["pull", "data/finl"], cwd=consumer)
    assert dvc.returncode != 0 and "does not exist as an output" in dvc.stdout + dvc.stderr
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    monkeypatch.chdir(consumer)
    assert cli.main(["data", "pull", "--path", str(consumer), "data/finl"]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "no default DVC remote" not in err and "does not exist as an output" in err


def test_an_empty_default_in_config_local_unsets_it_as_dvc_does(real_dvc, tmp_path: Path) -> None:
    """dvc merges `config.local` over `.dvc/config`, so an empty `remote =` there leaves no
    default, and push refuses. Mutation that must redden this: skip an empty value."""
    from mintd._dvc_state import default_remote_name

    root = _project(real_dvc, tmp_path, f"[core]\n    remote = storage\n{R}    url = s3://b/p\n", "[core]\n    remote =\n")
    assert _dvc_reads(real_dvc, root, "core.remote") == ""
    assert default_remote_name(root) is None


@pytest.mark.parametrize(("local_ok", "hint"), [
    pytest.param(True, "set a default: dvc remote default store (add --local to keep it out of git)", id="pull-and-clone"),
    pytest.param(False, "set a default: dvc remote default store, and commit .dvc/config", id="push-cache-and-publish"),
])
def test_the_no_default_hint_offers_local_only_where_a_local_default_serves(tmp_path: Path, local_ok, hint) -> None:
    """A default kept in `config.local` serves pull and clone, but push's commit gate, publish
    and the storage check read the committed `.dvc/config`, so their hint says to commit it.
    Mutation that must redden this: one hint for every verb."""
    from mintd._dvc_state import require_default_remote

    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text('[\'remote "store"\']\n    url = s3://b/p\n')
    with pytest.raises(MintdError) as raised:
        require_default_remote(tmp_path, local_ok=local_ok)
    assert raised.value.hint == hint


# --- a remote's name, as dvc compares it ---------------------------------------------------


def test_a_remote_named_in_mixed_case_is_one_remote_as_dvc_reads_it(real_dvc, tmp_path: Path, monkeypatch) -> None:
    """dvc lowercases a remote's name when it reads a config, and looks a named remote up
    lowercased, so `Storage` and `storage` are one remote. So does every lookup and
    comparison here: the storage check, the readers, push's default-remote check, and the
    storage check's repair, which dvc's own rename now clears.

    Mutations: compare metadata's name as given -> a mismatch the hint can't clear, red;
    look a name up as given -> KeyError and {}, red; compare push's --remote as given ->
    "isn't the default remote", red."""
    import json

    from mintd import _push_verify
    from mintd._storage_state import StorageState, inspect_storage, repair_hint

    root = _project(real_dvc, tmp_path, f"[core]\n    remote = Storage\n{R.replace('storage', 'Storage')}"
                                        "    url = s3://b/lab/p\n    version_aware = true\n")
    assert _dvc_reads(real_dvc, root, "remote.STORAGE.url") == "s3://b/lab/p"  # dvc finds it in any case
    meta = {"storage": {"bucket": "b", "prefix": "lab/p", "dvc": {"remote_name": "Storage"}}}
    (root / "metadata.json").write_text(json.dumps(meta))
    assert inspect_storage(root).state == StorageState.INITIALIZED
    assert get_remote_config(root, "Storage")["url"] == merged_remote(root, "STORAGE")["url"] == "s3://b/lab/p"

    class Reached(Exception):
        pass

    def reached(*_a, **_k):
        raise Reached  # past the default-remote check, at the S3 client

    monkeypatch.setattr(_push_verify, "_create_s3_client", reached)
    with pytest.raises(Reached):
        _push_verify.prepare(root, remote_arg="Storage", selection=None, aws_profile=None)

    # The storage check's repair: dvc's rename writes the name as typed, and mintd reads it lowercased.
    (root / ".dvc" / "config").write_text(f"[core]\n    remote = storage\n{R}    url = s3://b/lab/p\n")
    meta["storage"]["dvc"]["remote_name"] = "Other"
    (root / "metadata.json").write_text(json.dumps(meta))
    inspection = inspect_storage(root)
    assert inspection.state == StorageState.NAME_MISMATCH
    assert "dvc remote rename storage Other" in (repair_hint(inspection) or "")
    real_dvc(["remote", "rename", "storage", "Other"], cwd=root, check=True)
    assert inspect_storage(root).state == StorageState.INITIALIZED


@pytest.mark.parametrize("where", ["after-the-named-remote", "before-it"])
def test_a_remote_section_with_no_name_is_read_past_as_main_read_it(real_dvc, tmp_path: Path, where: str) -> None:
    """dvc stops on `[remote]` with no name (its schema wants a section per remote), and main's
    readers read past it. So does this one, instead of letting it replace the named remotes.

    Mutation: keep it as a section -> after the named remote, the readers lose it, red."""
    from mintd._storage_state import inspect_storage

    bare, named = "[remote]\n    url = s3://x/y\n", f"{R}    url = s3://b/p\n"
    body = named + bare if where == "after-the-named-remote" else bare + named
    root = _project(real_dvc, tmp_path, f"[core]\n    remote = storage\n{body}")
    assert real_dvc(["status"], cwd=root).returncode != 0
    assert get_remote_config(root, "storage") == merged_remote(root, "storage") == {"url": "s3://b/p"}
    assert inspect_storage(root).dvc_remote_name == "storage"


@pytest.mark.parametrize("level", ["config", "config.local"])
def test_check_reports_a_config_dvc_cannot_read_and_carries_on(tmp_path: Path, level: str) -> None:
    """Plain `mintd check` on a config dvc can't read: one finding naming the file, and every
    other finding still made, where it stopped mid-run with a traceback.

    Mutation: let the folder-list check's error escape -> check_project raises, red."""
    from mintd.check import check_project
    from tests._dvc_fixtures import stage_dvc_project

    stage_dvc_project(tmp_path)
    (tmp_path / ".dvc" / level).write_text("[core\n")
    findings = check_project(tmp_path)
    assert any(f.kind == "dvc_state_unreadable" and "can't read" in f.message and level in f.message
               for f in findings)
    assert any(f.kind == "data_products_output_untracked" for f in findings)  # the checks after it still ran
