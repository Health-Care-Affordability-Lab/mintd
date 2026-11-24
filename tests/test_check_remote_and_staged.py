"""`mintd check --remote` and `mintd check --staged`.

`--remote` asks S3, read-only, whether every pinned file version the working
tree and HEAD record is there, as the identity DVC pushes with (moto here,
handed in at `mintd.check._create_s3_client`, the S3 boundary). `--staged`
is offline: it refuses a commit that drops S3 version IDs from files whose
bytes it doesn't change, as `dvc commit -f <stage>` does. Both read git
relative to the project, with real git, so a project below its
repository's root reads the same as one at it.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from mintd import cli
from mintd._registry_git_ops import GitOpError
from mintd.check import check_project, check_staged
from tests._dvc_fixtures import REMOTE, stage_dvc_project

LAYOUTS = ["at-the-repo-root", "below-the-repo-root"]


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _git(proj: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=proj, check=True, capture_output=True)


def _repo(tmp_path: Path, *, version_aware: bool = True, layout: str = "at-the-repo-root") -> Path:
    """A git repo with a dvc project declaring `data/final/`: the repo
    itself, or `repo/proj/` below the repo's root."""
    top = tmp_path / ("proj" if layout == "at-the-repo-root" else "repo")
    top.mkdir()
    _git(top, "init", "-q", "-b", "main")
    _git(top, "config", "user.email", "t@t")
    _git(top, "config", "user.name", "T")
    proj = top if layout == "at-the-repo-root" else top / "proj"
    proj.mkdir(exist_ok=True)
    stage_dvc_project(proj, version_aware=version_aware)
    return proj


def _folder_pointer(proj: Path, name: str, files: dict[str, tuple[str, str | None]]) -> None:
    """`data/<name>.dvc` as dvc writes a version-aware folder out: each file's
    md5 and, when given, its version ID under the remote."""
    lines = ["outs:", "- hash: md5", f"  path: {name}", "  files:"]
    for rel, (md5, vid) in files.items():
        lines += [f"  - relpath: {rel}", f"    md5: {md5}", "    size: 2"]
        if vid:
            lines += ["    cloud:", f"      {REMOTE}:", f"        etag: {md5}", f"        version_id: {vid}"]
    (proj / "data").mkdir(exist_ok=True)
    (proj / "data" / f"{name}.dvc").write_text("\n".join(lines) + "\n")


def _commit(proj: Path, message: str = "c") -> None:
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", message)


def _file_pointer(md5: str, vid: str | None, *, path: str, remote: str = REMOTE, own_remote: bool = False) -> str:
    """A single-file `.dvc` pointer, its version ID (when given) under
    `remote`; `own_remote` names `remote` on the out itself."""
    cloud = f"  cloud:\n    {remote}:\n      etag: {md5}\n      version_id: {vid}\n" if vid else ""
    own = f"  remote: {remote}\n" if own_remote else ""
    return f"outs:\n- hash: md5\n  path: {path}\n  md5: {md5}\n  size: 2\n{own}{cloud}"


def _lock(stage: str, md5: str, vid: str | None, *, path: str = "data/x.csv") -> str:
    """`dvc.lock` with one stage and one file out, its version ID when given."""
    cloud = f"      cloud:\n        {REMOTE}:\n          etag: {md5}\n          version_id: {vid}\n" if vid else ""
    return (f"schema: '2.0'\nstages:\n  {stage}:\n    cmd: run\n    outs:\n    - path: {path}\n      hash: md5\n"
            f"      md5: {md5}\n      size: 2\n{cloud}")


#: `.dvc/config` shapes dvc refuses to push with: one remote and no default
#: (`data push --remote` can still have pinned files there), or a `version_aware`
#: or `worktree` that dvc reads as neither true nor false.
CONFIGS_DVC_REFUSES = ["no-default-remote", "a-note-after-true", "yes", "worktree-bogus"]
#: The shapes where the refused value is a version-aware setting.
VERSION_AWARE_REFUSED = CONFIGS_DVC_REFUSES[1:]


def _config_dvc_refuses(proj: Path, how: str) -> tuple[str, str]:
    """Rewrite `proj`'s `.dvc/config` as `how` says; the (message, hint) that names it."""
    cfg = proj / ".dvc" / "config"
    text = cfg.read_text(encoding="utf-8")
    if how == "no-default-remote":
        cfg.write_text(text.replace(f"[core]\n    remote = {REMOTE}\n", ""), encoding="utf-8")
        return ("no default DVC remote in .dvc/config",
                f"set a default: dvc remote default {REMOTE}, and commit .dvc/config")
    key, value = {"a-note-after-true": ("version_aware", "true ; note"), "yes": ("version_aware", "yes"),
                  "worktree-bogus": ("worktree", "bogus")}[how]  # worktree: beside `version_aware = true`
    line = f"version_aware = {value}" if key == "version_aware" else f"version_aware = true\n    worktree = {value}"
    cfg.write_text(text.replace("version_aware = true", line), encoding="utf-8")
    return (f"the DVC remote {REMOTE} sets {key} to {value!r} in .dvc/config, which dvc reads as neither true nor "
            "false, so dvc refuses the config in any clone of the project",
            f"set {key} to true or false in .dvc/config and commit it; a clone doesn't carry .dvc/config.local")


def _errors(findings) -> list[tuple[str, str, str]]:
    return [(f.kind, f.message, f.hint) for f in findings if f.severity == "error"]


def _remote(findings) -> list[tuple[str, str, str]]:
    return [(f.severity, f.kind, f.message) for f in findings if (f.kind or "").startswith("remote_")]


def _git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """From here on git can't read the repository: a `git` that refuses, as
    git does a checkout another user owns, or no git on PATH at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if how == "refuses":
        git = bin_dir / "git"
        git.write_bytes(b"#!/bin/sh\necho 'fatal: detected dubious ownership in repository' >&2\nexit 128\n")
        git.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


GIT_FAILS = [pytest.param("refuses", marks=pytest.mark.skipif(sys.platform == "win32", reason="Windows runs git.exe")),
             "missing"]


class _S3:
    """What `mintd.check._create_s3_client` hands out (`client`: the moto
    client, or a wrapper a test puts there) and the profile each call asked
    for."""

    def __init__(self, moto) -> None:
        self.moto = self.client = moto
        self.profiles: list[str | None] = []


class _Heads:
    """The moto client, with each `head_object` answered through `answer`
    (given the call's kwargs and the real call) and its key recorded."""

    def __init__(self, moto, answer=lambda kw, head: head()) -> None:
        self._moto, self._answer, self.keys = moto, answer, []

    def __getattr__(self, name: str):
        return getattr(self._moto, name)

    def head_object(self, **kw):
        self.keys.append(kw["Key"])
        return self._answer(kw, lambda: self._moto.head_object(**kw))


@pytest.fixture
def s3(s3_versioned, monkeypatch: pytest.MonkeyPatch):
    moto, bucket = s3_versioned
    seam = _S3(moto)

    def create(cfg, profile, **_):
        seam.profiles.append(profile)
        return seam.client

    monkeypatch.setattr("mintd.check._create_s3_client", create)
    return seam, bucket


def _pushed(tmp_path: Path, s3, *, layout: str = "at-the-repo-root"):
    """`data/final/` (a.csv, b.csv) on the versioned bucket, its pointer
    pinned to those versions and committed."""
    seam, bucket = s3
    proj = _repo(tmp_path, layout=layout)
    files = {}
    for rel, data in {"a.csv": b"a\n", "b.csv": b"b\n"}.items():
        put = seam.moto.put_object(Bucket=bucket, Key=f"lab/p/data/final/{rel}", Body=data)
        files[rel] = (_md5(data), put["VersionId"])
    _folder_pointer(proj, "final", files)
    _commit(proj)
    return proj, seam, bucket, files


@pytest.fixture
def pushed(tmp_path: Path, s3):
    return _pushed(tmp_path, s3)


def _broken(files):
    """a.csv pinned to b.csv's version, which a.csv's key doesn't hold."""
    return {"a.csv": (files["a.csv"][0], files["b.csv"][1]), "b.csv": files["b.csv"]}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_remote_finds_nothing_when_every_pinned_version_is_there(tmp_path: Path, s3, layout: str) -> None:
    proj, *_ = _pushed(tmp_path, s3, layout=layout)
    assert _remote(check_project(proj, remote=True)) == []


def test_remote_names_a_version_that_is_not_at_its_key(pushed) -> None:
    """S3 has no such version at a.csv's key, in the working tree and at
    HEAD alike, so one finding says both.

    Mutation: drop the missing verdict from the findings -> nothing, red.
    """
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", _broken(files))
    _commit(proj)
    (finding,) = [f for f in check_project(proj, remote=True) if f.kind == "remote_pin_missing"]
    assert finding.severity == "error"
    assert finding.message == ("data/final.dvc in the working tree and at HEAD: 1 file(s) where its recorded S3 "
                               "version isn't at its key: data/final/a.csv")
    assert finding.hint == "mintd data push re-attaches or re-uploads them"  # the working tree is wrong too


def test_remote_names_a_version_that_holds_other_bytes(pushed) -> None:
    proj, seam, bucket, files = pushed
    other = seam.moto.put_object(Bucket=bucket, Key="lab/p/data/final/a.csv", Body=b"z\n")
    _folder_pointer(proj, "final", {"a.csv": (files["a.csv"][0], other["VersionId"]), "b.csv": files["b.csv"]})
    _commit(proj)
    (finding,) = _remote(check_project(proj, remote=True))
    assert finding[:2] == ("error", "remote_pin_wrong_bytes") and "data/final/a.csv" in finding[2]


def test_remote_names_a_null_version(pushed) -> None:
    """S3 answers a.csv's HEAD with version `null`, as it does once
    versioning is suspended (moto can't, so the answer is rewritten).

    Mutation: map unpinned to a warning -> red.
    """
    proj, seam, *_ = pushed
    seam.client = _Heads(seam.moto, lambda kw, head: {**head(), "VersionId": "null"}
                         if kw["Key"].endswith("/a.csv") else head())
    (finding,) = _remote(check_project(proj, remote=True))
    assert finding[:2] == ("error", "remote_pin_unpinned") and "data/final/a.csv" in finding[2]


def _raise(exc: Exception):
    raise exc


@pytest.mark.parametrize("answer, why", [
    (lambda head: _raise(ClientError({"Error": {"Code": "InvalidArgument"},
                                      "ResponseMetadata": {"HTTPStatusCode": 400}}, "HeadObject")),
     "S3 gave no clear answer for its recorded version"),
    (lambda head: {**head(), "ETag": '"0123-2"', "ContentLength": 99},
     "its multipart upload can't be proven without the file in the cache"),
    (lambda head: {**head(), "ETag": '"0123-2"'}, "its multipart upload matches by size only"),
], ids=["inconclusive", "unproven", "size_only"])
def test_remote_names_a_pin_s3_cant_settle(pushed, answer, why: str) -> None:
    """S3 gives an error that maps to nothing: the version is unproven, an
    error. A multipart ETag with the file not in the cache (a.csv's 2 bytes:
    by size only when S3's size matches) is a warning: the version is there,
    and nothing proves its bytes wrong, nor right.

    Mutations: drop a verdict -> nothing reported, red; change one's
    severity -> red.
    """
    proj, seam, *_ = pushed
    seam.client = _Heads(seam.moto, lambda kw, head: answer(head) if kw["Key"].endswith("/a.csv") else head())
    severity = "error" if why.startswith("S3 gave") else "warning"
    assert _remote(check_project(proj, remote=True)) == [(
        severity, "remote_unchecked",
        f"data/final.dvc in the working tree and at HEAD: 1 file(s) where {why}: data/final/a.csv")]
    assert cli.main(["check", "--remote", str(proj)]) == (1 if severity == "error" else 0)


def test_remote_fails_when_s3_cant_be_reached(pushed) -> None:
    """Every HEAD fails to connect, past the retries: no pin is proven, so
    the check fails rather than pass with a warning.

    Mutation: map inconclusive to a warning -> exit 0, red.
    """
    from botocore.exceptions import EndpointConnectionError

    proj, seam, *_ = pushed

    def down(kw, head):
        raise EndpointConnectionError(endpoint_url="https://s3.example")

    seam.client = _Heads(seam.moto, down)
    (finding,) = _remote(check_project(proj, remote=True))
    assert finding[:2] == ("error", "remote_unchecked")
    assert "S3 gave no clear answer" in finding[2] and "data/final/a.csv, data/final/b.csv" in finding[2]
    assert cli.main(["check", "--remote", str(proj)]) == 1


@pytest.mark.parametrize("all_outs", [False, True], ids=["declared", "all"])
def test_remote_refuses_an_out_with_its_own_remote(pushed, all_outs: bool) -> None:
    """Push refuses an out that names a remote of its own on a version-aware
    remote, so `--remote` can't check it as push's identity: an error,
    declared (`data/final`) or, under `--all`, not (`data/extra`).

    Mutation: drop the own-remote refusal from `pinned_remote` -> the
    undeclared one passes, red.
    """
    proj, _, _, files = pushed
    out = "final" if not all_outs else "extra"
    (proj / "data" / f"{out}.dvc").write_text(_file_pointer(files["a.csv"][0], "gone", path=out, remote="other",
                                                           own_remote=True))
    _commit(proj)
    (finding,) = [f for f in check_project(proj, remote=True, all_outs=all_outs) if f.kind == "remote_unreadable"]
    assert finding.severity == "error"
    assert finding.message == ("outputs with their own remote aren't supported on a version-aware remote: "
                               f"data/{out}.dvc: {out}")


@pytest.mark.parametrize("shape", ["files-without-ids", "bare-tree"])
def test_remote_names_an_undeclared_out_with_no_ids_only_with_all(pushed, shape: str) -> None:
    """No offline finding covers an undeclared out, so under `--all` one
    that records no version ID (files listed without one, or a bare tree
    hash) is an error; without `--all` it isn't checked at all.

    Mutations: report an entry with no ID only at HEAD where the working
    tree has it -> files-without-ids passes, red; the same for a bare tree
    -> bare-tree red.
    """
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    proj, _, _, files = pushed
    if shape == "files-without-ids":
        _folder_pointer(proj, "extra", {"a.csv": (files["a.csv"][0], None)})
        named = "data/extra/a.csv"
    else:
        tree = dir_manifest_md5([DvcFileEntry(md5=files["a.csv"][0], relpath="a.csv", size=2)])
        (proj / "data" / "extra.dvc").write_text(f"outs:\n- md5: {tree}\n  size: 2\n  nfiles: 1\n  hash: md5\n"
                                                "  path: extra\n")
        named = "data/extra"
    _commit(proj)
    assert _remote(check_project(proj, remote=True)) == []
    assert _remote(check_project(proj, remote=True, all_outs=True)) == [(
        "error", "remote_pin_unpinned", "data/extra.dvc in the working tree and at HEAD: 1 file(s) where it "
        f"records no S3 version ID, so importers can't fetch it: {named}")]


@pytest.mark.parametrize("committed", [False, True], ids=["in-the-working-tree", "at-head-only"])
def test_remote_names_a_nested_dvc_lock_it_doesnt_read(pushed, committed: bool) -> None:
    """Only the root `dvc.lock` is read, as push verification reads it: a
    nested one, in the working tree or only at HEAD, is named, not passed.

    Mutation: drop the nested-lock finding -> nothing, red; read nested
    locks from the working tree only -> at-head-only red.
    """
    proj, *_ = pushed
    (proj / "pipe").mkdir()
    (proj / "pipe" / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - x.csv\n")
    (proj / "pipe" / "dvc.lock").write_text(_lock("build", _md5(b"x\n"), "gone", path="x.csv"))
    _commit(proj)
    if committed:
        _git(proj, "rm", "-q", "--cached", "-r", "pipe")
        import shutil

        shutil.rmtree(proj / "pipe")
        _git(proj, "reset", "-q")  # unstage the removal: HEAD still has pipe/, the working tree doesn't
    assert _remote(check_project(proj, remote=True, all_outs=True)) == [(
        "warning", "remote_unchecked", "only the root dvc.lock is read, so the pins in pipe/dvc.lock aren't checked")]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("bad_at", ["working-tree", "head"])
def test_remote_checks_the_working_tree_and_head_and_says_which(tmp_path: Path, s3, bad_at: str, layout: str) -> None:
    """Importers read the pushed commit, so HEAD is checked as well as the
    working tree, read relative to the project wherever it sits in its
    repository, and each finding says which tree it comes from.

    Mutations: check only the working tree -> the HEAD case finds nothing,
    red; read HEAD by repository path -> below the root, nothing at HEAD,
    red.
    """
    proj, _, _, files = _pushed(tmp_path, s3, layout=layout)
    _folder_pointer(proj, "final", _broken(files))
    if bad_at == "head":
        _commit(proj)
        _folder_pointer(proj, "final", files)
    (finding,) = [f for f in check_project(proj, remote=True) if f.kind == "remote_pin_missing"]
    where = "at HEAD" if bad_at == "head" else "in the working tree"
    assert finding.message.startswith(f"data/final.dvc {where}: 1 file(s)"), finding.message
    assert ("push the pointers again" in (finding.hint or "")) is (bad_at == "head")


def test_remote_checks_only_the_working_tree_before_the_first_commit(tmp_path: Path, s3) -> None:
    """No commit yet: nothing at HEAD to read, and no error for that.

    Mutation: read "no commit yet" as a git failure -> raises, red.
    """
    seam, bucket = s3
    proj = _repo(tmp_path)
    put = seam.moto.put_object(Bucket=bucket, Key="lab/p/data/final/a.csv", Body=b"a\n")
    _folder_pointer(proj, "final", {"a.csv": (_md5(b"a\n"), "not-" + put["VersionId"])})
    assert _remote(check_project(proj, remote=True)) == [
        ("error", "remote_pin_missing", "data/final.dvc in the working tree: 1 file(s) where its recorded S3 "
                                        "version isn't at its key: data/final/a.csv")]


@pytest.mark.parametrize("how", GIT_FAILS)
def test_remote_fails_when_git_cant_read_the_repository(pushed, tmp_path: Path, monkeypatch, how: str) -> None:
    """HEAD can't be read, so the check can't pass: it raises, and the CLI
    exits non-zero.

    Mutation: read any git failure as "no commit yet" -> HEAD skipped,
    nothing raised, red.
    """
    proj, *_ = pushed
    _git_fails(tmp_path, monkeypatch, how)
    with pytest.raises(GitOpError):
        check_project(proj, remote=True)
    assert cli.main(["check", "--remote", str(proj)]) != 0


@pytest.mark.parametrize("env, named", [(None, "the default AWS credentials"), ("lab", "profile 'lab'"),
                                        ("", "profile ''")], ids=["no-profile", "named", "empty-name"])
def test_remote_reports_access_denied_as_one_finding_naming_the_profile(
    pushed, monkeypatch: pytest.MonkeyPatch, env: str | None, named: str,
) -> None:
    """A 403 on HEAD is one finding naming the profile tried; an empty name
    is a name (`resolve_profile`: dvc names profile '' then).

    Mutations: let S3AccessDenied out -> a traceback, red; read an empty
    name as none -> empty-name red.
    """
    def denied(kw, head):
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"},
                           "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject")

    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    if env is None:
        monkeypatch.delenv("AWS_PROFILE", raising=False)
    else:
        monkeypatch.setenv("AWS_PROFILE", env)
    proj, seam, *_ = pushed
    seam.client = _Heads(seam.moto, denied)
    (finding,) = _remote(check_project(proj, remote=True))
    assert finding[:2] == ("error", "remote_unreadable")
    assert finding[2].startswith(f"can't check the remote {REMOTE} as {named}: ") and "HeadObject" in finding[2]


def test_remote_on_an_md5_layout_remote_checks_nothing(tmp_path: Path) -> None:
    proj = _repo(tmp_path, version_aware=False)
    assert _remote(check_project(proj, remote=True)) == [
        ("info", "remote_not_checked",
         "the default remote isn't a version-aware S3 remote, so nothing is pinned by version to check")]


@pytest.mark.parametrize("how", CONFIGS_DVC_REFUSES)
def test_remote_refuses_a_remote_it_cant_read_as_dvc_does(pushed, how: str) -> None:
    """A pin that isn't at its key, behind a config dvc refuses: an error that
    names the config, not "nothing is pinned by version".

    Mutations: read a missing default as "no remote" -> the info, red; read
    `true ; note` or `yes` as not version-aware -> the info, red.
    """
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", _broken(files))
    _commit(proj)
    message, hint = _config_dvc_refuses(proj, how)
    findings = check_project(proj, remote=True)
    assert not [f for f in findings if f.kind == "remote_not_checked"]
    if how == "no-default-remote":  # the storage check names the missing default too, as before this lane
        assert _errors(findings) == [
            ("storage_partial_meta_only", "storage drift detected: partial_meta_only",
             f".dvc/config has remote '{REMOTE}' but sets no default remote. Run: dvc remote default {REMOTE}"),
            ("remote_unreadable", message, hint)]
    else:
        assert _errors(findings) == [("dvc_state_unreadable", message, hint)]


@pytest.mark.parametrize("remote", [False, True], ids=["check", "check-remote"])
@pytest.mark.parametrize("how", VERSION_AWARE_REFUSED)
def test_a_version_aware_dvc_refuses_is_one_finding(pushed, how: str, remote: bool) -> None:
    """Unpinned files behind `version_aware = yes`: the one error says dvc refuses
    the value, where check also said the folder can't be delivered, the remote
    "is version_aware", beside it.

    Mutations: read `yes` as true in the storage check -> a second error, red;
    drop the offline check's finding -> none in plain check, red; say the
    remote lane's again -> two under --remote, red; read `worktree` only when
    `version_aware` is unset -> none for `worktree = bogus`, red.
    """
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _commit(proj)
    message, hint = _config_dvc_refuses(proj, how)
    assert _errors(check_project(proj, remote=remote)) == [("dvc_state_unreadable", message, hint)]


def test_a_committed_value_dvc_refuses_is_refused_under_a_local_override(pushed) -> None:
    """`yes` in .dvc/config under `true` in .dvc/config.local: dvc accepts it here, but
    a clone carries only .dvc/config. Check still refuses, says so, and offers the fix
    that clears it: a commit, not config.local.

    Mutation: read the override in the storage check -> no error, red.
    """
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _commit(proj)
    message, hint = _config_dvc_refuses(proj, "yes")
    (proj / ".dvc" / "config.local").write_text(f"['remote \"{REMOTE}\"']\n    version_aware = true\n",
                                               encoding="utf-8")
    assert _errors(check_project(proj)) == [("dvc_state_unreadable", message, hint)]


def test_remote_with_no_remote_at_all_says_so(tmp_path: Path) -> None:
    proj = _repo(tmp_path)
    (proj / ".dvc" / "config").write_text("")
    assert _remote(check_project(proj, remote=True)) == [
        ("info", "remote_not_checked", "the project has no DVC remote, so nothing is pinned by version to check")]


@pytest.mark.parametrize("how", VERSION_AWARE_REFUSED)
def test_push_refuses_a_version_aware_dvc_reads_as_neither_true_nor_false(tmp_path: Path, how: str) -> None:
    """The same reading as check's: push stops before any S3 call, where
    reading it as not version-aware would skip verifying the pins.

    Mutation: drop the check from `_version_aware` -> prepare returns None, red.
    """
    from mintd._push_verify import PushRefused, prepare

    proj = _repo(tmp_path)
    message, hint = _config_dvc_refuses(proj, how)
    with pytest.raises(PushRefused) as exc:
        prepare(proj, remote_arg=None, selection=None, aws_profile=None)
    assert (str(exc.value), exc.value.hint) == (message, hint)


def test_push_names_dvc_config_when_config_local_sets_only_another_key(tmp_path: Path) -> None:
    """`yes` in .dvc/config, and a per-machine `profile` in .dvc/config.local for the
    same remote: the refused value is still the committed one, so the fix is a commit.

    Mutation: name config.local when its section sets any key -> the local hint, red.
    """
    from mintd._push_verify import PushRefused, prepare

    proj = _repo(tmp_path)
    message, hint = _config_dvc_refuses(proj, "yes")
    (proj / ".dvc" / "config.local").write_text(f"['remote \"{REMOTE}\"']\n    profile = lab\n", encoding="utf-8")
    with pytest.raises(PushRefused) as exc:
        prepare(proj, remote_arg=None, selection=None, aws_profile=None)
    assert (str(exc.value), exc.value.hint) == (message, hint)


def test_push_names_config_local_when_the_value_dvc_refuses_is_there(tmp_path: Path) -> None:
    """`yes` in .dvc/config.local over `true` in .dvc/config: dvc refuses it on this
    machine only, so the fix is in config.local, and a commit wouldn't clear it.

    Mutation: name .dvc/config whatever file holds the value -> the commit hint, red.
    """
    from mintd._push_verify import PushRefused, prepare

    proj = _repo(tmp_path)
    (proj / ".dvc" / "config.local").write_text(f"['remote \"{REMOTE}\"']\n    version_aware = yes\n",
                                               encoding="utf-8")
    with pytest.raises(PushRefused) as exc:
        prepare(proj, remote_arg=None, selection=None, aws_profile=None)
    assert (str(exc.value), exc.value.hint) == (
        f"the DVC remote {REMOTE} sets version_aware to 'yes' in .dvc/config.local, which dvc reads as neither true "
        "nor false, so dvc refuses the config on this machine", "set version_aware to true or false in .dvc/config.local")


def test_push_to_a_named_remote_with_no_default_names_the_missing_default(tmp_path: Path) -> None:
    """`--remote storage` on a version-aware remote with no `core.remote`: pins are read back
    through the default, so push stops with dvc's missing-default message and hint, not
    "isn't the default remote None" with a hint that fails next.

    Mutation: drop prepare's no-default branch -> the None message, red.
    """
    from mintd._push_verify import PushRefused, prepare

    proj = _repo(tmp_path)
    message, hint = _config_dvc_refuses(proj, "no-default-remote")
    with pytest.raises(PushRefused) as exc:
        prepare(proj, remote_arg="storage", selection=None, aws_profile=None)
    assert (str(exc.value), exc.value.hint) == (message, hint)


def test_remote_refuses_a_remote_with_its_own_keys(pushed) -> None:
    proj, *_ = pushed
    (proj / ".dvc" / "config.local").write_text(f'[remote "{REMOTE}"]\n    access_key_id = AKIA\n')
    (finding,) = _remote(check_project(proj, remote=True))
    assert finding[:2] == ("error", "remote_unreadable") and "its own access keys" in finding[2]


def test_remote_checks_an_undeclared_output_only_with_all(pushed) -> None:
    """Mutation: ignore the declared outputs -> the undeclared one is
    reported without --all, red."""
    proj, _, _, files = pushed
    _folder_pointer(proj, "other", {"a.csv": (files["a.csv"][0], files["b.csv"][1])})
    _commit(proj)
    assert _remote(check_project(proj, remote=True)) == []
    (finding,) = _remote(check_project(proj, remote=True, all_outs=True))
    assert finding[2].startswith("data/other.dvc in the working tree and at HEAD")


def test_check_remote_exits_1_on_an_error_and_all_needs_remote(pushed) -> None:
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", _broken(files))
    assert cli.main(["check", "--remote", str(proj)]) == 1
    with pytest.raises(SystemExit):
        cli.main(["check", "--all", str(proj)])


@pytest.mark.parametrize("flag, said", [("--all", "--all goes with --remote"),
                                        ("--upgrades", "--upgrades doesn't go with --staged")])
def test_check_staged_refuses_a_flag_it_would_ignore(tmp_path: Path, flag: str, said: str) -> None:
    """A flag `--staged` doesn't act on would read as checked.

    Mutation: drop either refusal -> the check runs and exits 0, red.
    """
    proj, _ = _pinned_repo(tmp_path)
    with pytest.raises(SystemExit, match=said):
        cli.main(["check", "--staged", flag, str(proj)])


def test_remote_judges_each_tree_by_its_own_bytes(pushed) -> None:
    """HEAD records a.csv's version with other bytes' md5, the working tree
    with the right one: the finding is HEAD's alone.

    Mutation: prove each pin once, whatever md5 a tree records -> the HEAD
    md5 is never judged, or blamed on both trees, red.
    """
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", {"a.csv": (_md5(b"z\n"), files["a.csv"][1]), "b.csv": files["b.csv"]})
    _commit(proj)
    _folder_pointer(proj, "final", files)
    (finding,) = _remote(check_project(proj, remote=True))
    assert finding[:2] == ("error", "remote_pin_wrong_bytes")
    assert finding[2].startswith("data/final.dvc at HEAD: 1 file(s)")


def test_remote_checks_a_pointer_that_is_only_at_head(pushed) -> None:
    """Deleted from the working tree but not yet committed: importers still
    read it from HEAD.

    Mutation: read HEAD only for the working tree's pointers -> nothing,
    red.
    """
    proj, _, _, files = pushed
    _folder_pointer(proj, "final", _broken(files))
    _commit(proj)
    (proj / "data" / "final.dvc").unlink()
    (finding,) = [f for f in check_project(proj, remote=True) if f.kind == "remote_pin_missing"]
    assert finding.message.startswith("data/final.dvc at HEAD: 1 file(s)")
    assert finding.hint == ("data/final.dvc is gone from the working tree: commit its removal, or restore it and run "
                            "mintd data push")  # push reads the working tree's pointers only


def test_remote_probes_as_the_profile_push_uses(pushed) -> None:
    """Push hands dvc mintd's AWS profile; the check asks S3 as the same one.

    Mutation: create the client with no profile -> red.
    """
    proj, seam, *_ = pushed
    assert _remote(check_project(proj, remote=True, aws_profile="lab")) == []
    assert seam.profiles == ["lab"]


def test_check_remote_cli_probes_as_mintds_profile(pushed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `[mintd]` section in the shared credentials file is the profile push
    uses (`Config.aws_profile_name`); the CLI hands it to the check.

    Mutation: drop aws_profile from the CLI's check_project call -> red.
    """
    proj, seam, *_ = pushed
    creds = tmp_path / "credentials"
    creds.write_text("[mintd]\naws_access_key_id = test\naws_secret_access_key = test\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    assert cli.main(["check", "--remote", str(proj)]) == 0
    assert seam.profiles == ["mintd"]


@pytest.mark.parametrize("how", ["an-uncommitted-gitignore-line", "force-added-under-an-ignored-folder"])
def test_remote_reads_every_committed_pointer_whatever_gitignore_says(pushed, how: str) -> None:
    """A committed pointer is tracked, and git never ignores a tracked
    file: HEAD's check reads it whatever `.gitignore` says, so a line not
    yet committed can't hide HEAD's stale pin, and nor can a folder ignored
    around a pointer that was force-added.

    Mutations: filter HEAD's pointers by the working tree's `.gitignore` ->
    nothing reported, red; drop the ignored pointer's hint -> push is
    named, red.
    """
    proj, _, _, files = pushed
    rel = "data/extra.dvc" if how == "an-uncommitted-gitignore-line" else "scratch/old.dvc"
    (proj / rel).parent.mkdir(exist_ok=True)
    (proj / rel).write_text(  # a.csv pinned to b.csv's version, which a.csv's key doesn't hold
        f"outs:\n- hash: md5\n  path: {Path(rel).stem}\n  files:\n  - relpath: a.csv\n    md5: {files['a.csv'][0]}\n"
        f"    size: 2\n    cloud:\n      {REMOTE}:\n        etag: {files['a.csv'][0]}\n"
        f"        version_id: {files['b.csv'][1]}\n")
    if how == "force-added-under-an-ignored-folder":
        with (proj / ".gitignore").open("a") as f:
            f.write("/scratch/\n")
        _git(proj, "add", "-f", rel)
    _commit(proj)
    if how == "an-uncommitted-gitignore-line":
        with (proj / ".gitignore").open("a") as f:
            f.write(f"/{rel}\n")
    (finding,) = [f for f in check_project(proj, remote=True, all_outs=True) if f.kind == "remote_pin_missing"]
    assert finding.message.startswith(f"{rel} at HEAD: 1 file(s) where its recorded S3 version isn't at its key")
    # push skips an ignored pointer, so the hint can't be push
    assert finding.hint == (f"{rel} is excluded by .gitignore, so push skips it: stop ignoring it and run mintd "
                            "data push, or git rm it")


def test_remote_heads_every_pin_rather_than_trust_a_memo(tmp_path: Path, s3) -> None:
    """A proof memo skips the HEAD of pins it remembers (all but a sample of
    16); `--remote` uses none, so nothing remembered can hide a deletion.
    Twenty pins, twenty HEADs.

    Mutation: hand prove_pins a memo that remembers every pin -> 16 HEADs,
    red.
    """
    seam, bucket = s3
    proj = _repo(tmp_path)
    files = {}
    for i in range(20):
        data = f"{i}\n".encode()
        put = seam.moto.put_object(Bucket=bucket, Key=f"lab/p/data/final/f{i:02}.csv", Body=data)
        files[f"f{i:02}.csv"] = (_md5(data), put["VersionId"])
    _folder_pointer(proj, "final", files)
    _commit(proj)
    seam.client = heads = _Heads(seam.moto)
    assert _remote(check_project(proj, remote=True)) == []
    assert len(heads.keys) == 20


@pytest.mark.parametrize("head_shape", ["files-without-ids", "bare-tree"])
def test_remote_names_a_head_that_lost_its_ids_while_the_working_tree_has_them(pushed, head_shape: str) -> None:
    """What `dvc commit -f` does, committed: HEAD records the files with no
    version IDs (or, for a folder out, a bare tree hash with no file list),
    while the working tree has them back. Nothing offline reads HEAD, so
    this is the check that sees it.

    Mutations: skip entries with no ID at HEAD -> files-without-ids red;
    skip HEAD's unexpandable folder outs -> bare-tree red.
    """
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    proj, _, _, files = pushed
    if head_shape == "files-without-ids":
        _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    else:
        tree = dir_manifest_md5([DvcFileEntry(md5=m, relpath=r, size=2) for r, (m, _) in files.items()])
        (proj / "data" / "final.dvc").write_text(f"outs:\n- md5: {tree}\n  size: 4\n  nfiles: 2\n  hash: md5\n"
                                                "  path: final\n")
    _commit(proj)
    _folder_pointer(proj, "final", files)
    (finding,) = [f for f in check_project(proj, remote=True) if f.kind == "remote_pin_unpinned"]
    assert finding.severity == "error"
    assert finding.message.startswith("data/final.dvc at HEAD: ")
    assert "records no S3 version ID, so importers can't fetch it" in finding.message
    assert "push the pointers again" in (finding.hint or "")


@pytest.mark.parametrize("shape", ["files-without-ids", "bare-tree", "gone-from-the-working-tree"])
def test_remote_reports_head_s_missing_ids_only_where_the_working_tree_has_them(pushed, shape: str) -> None:
    """HEAD lacks the IDs and so does the working tree's copy (or it has
    none): the offline unpinned finding's, never reported twice, and the
    "at HEAD" label would wrongly say the working tree is right.

    Mutations: report HEAD's missing IDs unless the working tree's copy is
    unpinned too -> gone-from-the-working-tree red; report every bare tree
    at HEAD -> bare-tree red.
    """
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    proj, _, _, files = pushed
    if shape == "bare-tree":
        tree = dir_manifest_md5([DvcFileEntry(md5=m, relpath=r, size=2) for r, (m, _) in files.items()])
        (proj / "data" / "final.dvc").write_text(f"outs:\n- md5: {tree}\n  size: 4\n  nfiles: 2\n  hash: md5\n"
                                                "  path: final\n")
    else:
        _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _commit(proj)
    if shape == "gone-from-the-working-tree":
        (proj / "data" / "final.dvc").unlink()
    found = check_project(proj, remote=True)
    assert _remote(found) == []
    if shape != "gone-from-the-working-tree":
        assert any(f.kind == "data_products_output_unpinned" for f in found)


def _damaged(good: str, damage: str) -> bytes:
    """A pointer dvc would refuse: a merge's conflict markers, cut short
    before its out's path (the cut-short rule's case), not UTF-8 (Latin-1,
    as an editor may save it), with a key twice (PyYAML takes it, dvc's
    ruamel refuses it), or an impossible date (ruamel raises ValueError)."""
    return {"conflict-markers": f"<<<<<<< ours\n{good}=======\n{good}>>>>>>> theirs\n".encode(),
            "cut-short": good[:good.index("  path:")].encode(),
            "not-utf-8": b"# caf\xe9\n" + good.encode(),
            "duplicate-key": f"{good}meta: 1\nmeta: 2\n".encode(),
            "impossible-date": f"{good}meta:\n  reviewed: 2023-02-29\n".encode()}[damage]


@pytest.mark.parametrize("damage", ["conflict-markers", "not-utf-8", "duplicate-key", "impossible-date"])
def test_remote_names_a_pointer_it_cant_read_at_head(pushed, damage: str) -> None:
    """A merge left conflict markers in HEAD's pointer (or it was saved as
    Latin-1) and the working tree was fixed but not committed: importers
    read the damaged one.

    Mutations: skip the screen at HEAD -> read as no outs, nothing, red;
    write the scratch tree without surrogateescape -> UnicodeEncodeError,
    red.
    """
    proj, *_ = pushed
    good = (proj / "data" / "final.dvc").read_text()
    (proj / "data" / "final.dvc").write_bytes(_damaged(good, damage))
    _commit(proj)
    (proj / "data" / "final.dvc").write_text(good)
    (finding,) = [f for f in check_project(proj, remote=True) if f.kind == "dvc_state_unreadable"]
    assert finding.severity == "error" and finding.message.startswith("could not read data/final.dvc at HEAD: ")
    if damage == "not-utf-8":
        assert finding.message.endswith(": not UTF-8 text (dvc writes UTF-8)")


@pytest.mark.parametrize("where", ["a-pointer", "dvc-yaml"])
def test_remote_names_a_working_tree_file_of_the_wrong_shape(pushed, where: str) -> None:
    """`outs` that isn't a list, in a working-tree pointer or a dvc.yaml
    stage, is the offline screen's to name: reading the remote as push's
    identity skips it rather than traceback.

    Mutation: drop `_own_remotes`'s list test -> TypeError, red.
    """
    proj, *_ = pushed
    if where == "a-pointer":
        (proj / "data" / "final.dvc").write_text("outs: true\n")
    else:
        (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs: 5\n")
    kinds = [f.kind for f in check_project(proj, remote=True)]
    assert "dvc_state_unreadable" in kinds


def test_remote_leaves_out_a_committed_subrepo_s_pointers(pushed) -> None:
    """A folder with its own `.dvc/` is a project of its own, which push
    from here doesn't read; neither does HEAD's check.

    Mutation: read every committed pointer at HEAD, subrepo or not -> its
    stale pin is reported, red.
    """
    proj, _, _, files = pushed
    sub = proj / "sub"
    (sub / ".dvc").mkdir(parents=True)
    (sub / ".dvc" / "config").write_text(f"[core]\n    remote = {REMOTE}\n")
    _folder_pointer(sub, "final", _broken(files))
    _commit(proj)
    assert _remote(check_project(proj, remote=True, all_outs=True)) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't name a file with a quote")
def test_remote_reads_a_head_only_pointer_whose_name_git_would_quote(pushed) -> None:
    """Mutation: list HEAD's files without -z -> `"data/we\\"ird.dvc"`
    doesn't end in .dvc and is skipped, red."""
    proj, _, _, files = pushed
    _folder_pointer(proj, 'we"ird', _broken(files))
    _commit(proj)
    (proj / "data" / 'we"ird.dvc').unlink()
    (finding,) = [f for f in check_project(proj, remote=True, all_outs=True) if f.kind == "remote_pin_missing"]
    assert finding.message.startswith('data/we"ird.dvc at HEAD: ')


# --- check --staged ---------------------------------------------------------

def _pinned_repo(tmp_path: Path, *, layout: str = "at-the-repo-root") -> tuple[Path, dict[str, tuple[str, str | None]]]:
    proj = _repo(tmp_path, layout=layout)
    files: dict[str, tuple[str, str | None]] = {"a.csv": (_md5(b"a\n"), "va"), "b.csv": (_md5(b"b\n"), "vb")}
    _folder_pointer(proj, "final", files)
    _commit(proj)
    return proj, files


@pytest.mark.parametrize("layout", LAYOUTS)
def test_staged_refuses_a_commit_that_drops_ids_from_unchanged_files(tmp_path: Path, layout: str) -> None:
    """What `dvc commit -f <stage>` does: the same md5s, no cloud blocks.

    Mutations: compare the staged pointer with itself -> nothing, red;
    refuse a lost ID only when the md5 changed -> nothing, red; read the
    index by repository path -> below the root, nothing staged matches,
    red.
    """
    proj, files = _pinned_repo(tmp_path, layout=layout)
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "data/final.dvc")
    (finding,) = check_staged(proj)
    assert (finding.severity, finding.kind) == ("error", "staged_drops_version_ids")
    assert finding.message == ("data/final.dvc drops S3 version IDs from 2 unchanged file(s) "
                               "(data/final/a.csv, data/final/b.csv)")
    assert finding.hint == "run mintd data push, which puts them back, then commit"


@pytest.mark.parametrize("how", CONFIGS_DVC_REFUSES)
def test_staged_refuses_a_commit_it_cant_check_against_its_remote(tmp_path: Path, how: str) -> None:
    """A commit that drops IDs, behind a config dvc refuses to push with: the
    pins may be real, so the commit isn't passed unchecked.

    Mutations: return no finding when there's a remote but no default -> [],
    red; read `true ; note` or `yes` as not version-aware -> [], red.
    """
    proj, files = _pinned_repo(tmp_path)
    message, hint = _config_dvc_refuses(proj, how)
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "data/final.dvc")
    (finding,) = check_staged(proj)
    assert (finding.severity, finding.kind, finding.message, finding.hint) == (
        "error", "staged_unchecked", f"can't check the staged commit: {message}", hint)


@pytest.mark.parametrize("change", ["new-data", "ids-added", "file-removed", "new-pointer", "pointer-deleted",
                                    "readme", "another-project", "a-dvc-yaml-never-added"])
def test_staged_lets_through_a_commit_that_drops_no_id(tmp_path: Path, change: str) -> None:
    """A changed md5 (new data, no ID yet), added IDs, a removed file, a new
    pointer, a deleted pointer, a README, and a pointer of another project
    in the same repository drop no ID from this project's unchanged files.

    Mutations: refuse any lost ID, md5 changed or not -> new-data red; read
    the whole repository's staged paths -> another-project red.
    """
    proj, files = _pinned_repo(tmp_path, layout="below-the-repo-root")
    other = proj.parent / "other"
    other.mkdir()
    _folder_pointer(other, "final", {rel: (md5, f"o{rel}") for rel, (md5, _) in files.items()})
    _commit(proj)
    if change == "new-data":
        _folder_pointer(proj, "final", {"a.csv": (_md5(b"A\n"), None), "b.csv": files["b.csv"]})
    elif change == "ids-added":
        _folder_pointer(proj, "final", {**files, "c.csv": (_md5(b"c\n"), "vc")})
    elif change == "file-removed":
        _folder_pointer(proj, "final", {"b.csv": files["b.csv"]})
    elif change == "new-pointer":
        _folder_pointer(proj, "fresh", {"x.csv": (_md5(b"x\n"), None)})
    elif change == "pointer-deleted":
        _git(proj, "rm", "-q", "data/final.dvc")
    elif change == "readme":
        (proj.parent / "README.md").write_text("hi\n")
    elif change == "another-project":  # what dvc commit -f does, in the other project
        _folder_pointer(other, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    else:  # on disk, not in git: each tree reads its own dvc.yaml from git
        (proj / "dvc.yaml").write_text("stages: {}\n")
        _folder_pointer(proj, "final", {**files, "c.csv": (_md5(b"c\n"), "vc")})
        _git(proj, "add", "data/final.dvc")
        assert check_staged(proj) == []
        return
    _git(proj, "add", "-A")
    assert check_staged(proj) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_staged_passes_the_first_commit(tmp_path: Path, layout: str) -> None:
    """Mutation: read "no commit yet" as a git failure -> raises, red."""
    proj = _repo(tmp_path, layout=layout)
    _folder_pointer(proj, "final", {"a.csv": (_md5(b"a\n"), "va")})
    _git(proj, "add", "-A")
    assert check_staged(proj) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_staged_reads_a_lock_out_through_its_stage_wdir(tmp_path: Path, layout: str) -> None:
    """A `wdir: code` stage records `../data/x.csv`; only dvc.lock is
    staged, so dvc.yaml comes from the index into both trees, and the out
    resolves to data/x.csv in both.

    Mutations: leave dvc.yaml out of the trees -> the out resolves to
    `../data/x.csv` outside the project, is dropped, nothing found, red;
    list staged paths by repository path -> below the root, `proj/dvc.lock`
    isn't `dvc.lock`, red.
    """
    proj = _repo(tmp_path, layout=layout)
    (proj / "dvc.yaml").write_text("stages:\n  build:\n    wdir: code\n    cmd: run\n    outs:\n    - ../data/x.csv\n")
    md5 = _md5(b"x\n")
    lock = ("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: ../data/x.csv\n      hash: md5\n"
            f"      md5: {md5}\n      size: 2\n")
    (proj / "dvc.lock").write_text(lock + f"      cloud:\n        {REMOTE}:\n          etag: {md5}\n          version_id: vx\n")
    _commit(proj)
    (proj / "dvc.lock").write_text(lock)
    _git(proj, "add", "dvc.lock")
    (finding,) = check_staged(proj)
    assert finding.message == "dvc.lock drops S3 version IDs from 1 unchanged file(s) (data/x.csv)"


@pytest.mark.parametrize("same_tree", [True, False], ids=["same-files", "new-data"])
def test_staged_reads_dvc_commit_f_s_bare_tree_when_the_tree_isnt_cached(tmp_path: Path, same_tree: bool) -> None:
    """`dvc commit -f` writes a folder out back as `md5: <tree>.dir`, with no
    file list; with no tree in the local cache (CI, after `dvc gc`), the
    tree hash of HEAD's file list says whether the files are the same.

    Mutation: skip the bare-tree comparison -> same-files passes, red.
    """
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    proj, files = _pinned_repo(tmp_path)
    now = files if same_tree else {**files, "a.csv": (_md5(b"A\n"), None)}
    tree = dir_manifest_md5([DvcFileEntry(md5=m, relpath=r, size=2) for r, (m, _) in now.items()])
    (proj / "data" / "final.dvc").write_text(f"outs:\n- md5: {tree}\n  size: 4\n  nfiles: 2\n  hash: md5\n"
                                            "  path: final\n")
    _git(proj, "add", "data/final.dvc")
    assert [f.message for f in check_staged(proj)] == (
        ["data/final.dvc drops S3 version IDs from 2 unchanged file(s) (data/final/a.csv, data/final/b.csv)"]
        if same_tree else [])


@pytest.mark.parametrize("how", GIT_FAILS)
def test_staged_fails_when_git_cant_read_the_repository(tmp_path: Path, monkeypatch, how: str) -> None:
    """A check that can't run must not pass: it raises, and the CLI exits
    non-zero.

    Mutation: read any git failure as "nothing staged" or "no commit yet"
    -> [] and exit 0, red.
    """
    proj, files = _pinned_repo(tmp_path)
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "data/final.dvc")
    _git_fails(tmp_path, monkeypatch, how)
    with pytest.raises(GitOpError):
        check_staged(proj)
    assert cli.main(["check", "--staged", str(proj)]) != 0


def test_staged_names_only_the_file_that_lost_its_id(tmp_path: Path) -> None:
    """Mutation: refuse only when every file of an out lost its ID -> red."""
    proj, files = _pinned_repo(tmp_path)
    _folder_pointer(proj, "final", {"a.csv": (files["a.csv"][0], None), "b.csv": files["b.csv"]})
    _git(proj, "add", "data/final.dvc")
    assert [f.message for f in check_staged(proj)] == [
        "data/final.dvc drops S3 version IDs from 1 unchanged file(s) (data/final/a.csv)"]


@pytest.mark.parametrize("move", ["pointer-renamed", "stage-renamed", "out-moved-into-a-stage"])
def test_staged_matches_a_file_by_its_s3_identity_whatever_records_it(tmp_path: Path, move: str) -> None:
    """A file is matched by its path, its S3 key, so a dropped ID is found
    when the pointer is renamed, the stage is renamed, or the out moves from
    a `.dvc` file into a `dvc.lock` stage.

    Mutations: match on (pointer, stage, out, relpath) -> every case passes,
    red; read HEAD's copies of staged pointers only, not deleted ones ->
    pointer-renamed and out-moved red; give HEAD's tree the staged dvc.yaml
    -> HEAD's stage isn't declared there, stage-renamed red.
    """
    md5 = _md5(b"x\n")
    if move == "pointer-renamed":
        proj, files = _pinned_repo(tmp_path)
        _git(proj, "rm", "-q", "data/final.dvc")
        _folder_pointer(proj, "renamed", {rel: (m, None) for rel, (m, _) in files.items()})
        (proj / "data" / "renamed.dvc").write_text((proj / "data" / "renamed.dvc").read_text()
                                                   .replace("path: renamed", "path: final"))
        want = "data/renamed.dvc drops S3 version IDs from 2 unchanged file(s) (data/final/a.csv, data/final/b.csv)"
    else:
        proj = _repo(tmp_path)
        (proj / "data").mkdir()
        if move == "stage-renamed":
            (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - data/x.csv\n")
            (proj / "dvc.lock").write_text(_lock("build", md5, "vx"))
        else:
            (proj / "data" / "x.csv.dvc").write_text(_file_pointer(md5, "vx", path="x.csv"))
        _commit(proj)
        if move == "out-moved-into-a-stage":
            _git(proj, "rm", "-q", "data/x.csv.dvc")
        stage = "make" if move == "stage-renamed" else "build"
        (proj / "dvc.yaml").write_text(f"stages:\n  {stage}:\n    cmd: run\n    outs:\n    - data/x.csv\n")
        (proj / "dvc.lock").write_text(_lock(stage, md5, None))
        want = "dvc.lock drops S3 version IDs from 1 unchanged file(s) (data/x.csv)"
    _git(proj, "add", "-A")
    assert [f.message for f in check_staged(proj)] == [want]


@pytest.mark.parametrize("outer", ["version-aware", "not-version-aware", "no-default-remote"])
@pytest.mark.parametrize("where", ["the-outer-project", "the-nested-project"])
def test_staged_names_a_nested_project_s_pointers_from_the_outer_one(tmp_path: Path, where: str, outer: str) -> None:
    """A folder with its own `.dvc/` is a project of its own, with its own
    remote (here the same name, as mintd scaffolds give them): from the
    outer project, its staged pointers are named, not read under the
    outer's remote, whatever the outer's remote is; from inside it, they
    are checked.

    Mutations: drop the finding -> the outer passes, red; read them as the
    outer's -> a second, drop finding, red; name them after the outer's
    no-remote or not-version-aware exit -> those cases pass, red.
    """
    proj = _repo(tmp_path, version_aware=outer == "version-aware")
    if outer == "no-default-remote":
        (proj / ".dvc" / "config").write_text("")
    sub = proj / "sub"
    sub.mkdir()
    stage_dvc_project(sub)
    (sub / "x.dvc").write_text(_file_pointer(_md5(b"x\n"), "vx", path="x"))
    _commit(proj)
    (sub / "x.dvc").write_text(_file_pointer(_md5(b"x\n"), None, path="x"))
    _git(proj, "add", "-A")
    if where == "the-outer-project":
        (finding,) = check_staged(proj)
        assert (finding.kind, finding.message) == (
            "staged_unchecked", "can't check the staged commit: the commit stages pointers in another DVC project "
            "(sub/x.dvc)")
    else:
        assert [f.message for f in check_staged(sub)] == ["x.dvc drops S3 version IDs from 1 unchanged file(s) (x)"]


@pytest.mark.parametrize("change", ["re-run-and-pushed", "ids-dropped", "stage-moved-to-the-root"])
def test_staged_names_a_nested_dvc_lock_it_doesnt_read(tmp_path: Path, change: str) -> None:
    """Only the root `dvc.lock` is read: a nested one the commit changes,
    or deletes (its stages' IDs may have moved, here into the root lock), is
    named as a warning, not checked. Re-running a nested pipeline and
    pushing it is everyday work, so the commit isn't refused.

    Mutations: drop the warning -> [], red; name a nested lock only when
    it isn't deleted -> stage-moved-to-the-root passes, red; make it an
    error -> the re-run refused, red.
    """
    proj = _repo(tmp_path)
    md5 = _md5(b"x\n")
    (proj / "pipe").mkdir()
    (proj / "pipe" / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - x.csv\n")
    (proj / "pipe" / "dvc.lock").write_text(_lock("build", md5, "vx", path="x.csv"))
    _commit(proj)
    if change == "re-run-and-pushed":  # new data, every ID present
        (proj / "pipe" / "dvc.lock").write_text(_lock("build", _md5(b"y\n"), "vy", path="x.csv"))
    elif change == "ids-dropped":
        (proj / "pipe" / "dvc.lock").write_text(_lock("build", md5, None, path="x.csv"))
    else:
        _git(proj, "rm", "-q", "pipe/dvc.yaml", "pipe/dvc.lock")
        (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - pipe/x.csv\n")
        (proj / "dvc.lock").write_text(_lock("build", md5, None, path="pipe/x.csv"))
    _git(proj, "add", "-A")
    (finding,) = check_staged(proj)
    assert (finding.severity, finding.kind) == ("warning", "staged_unchecked")
    assert finding.message == "only the root dvc.lock is read, so the pins in pipe/dvc.lock aren't checked"
    assert cli.main(["check", "--staged", str(proj)]) == 0


@pytest.mark.parametrize("shape", ["its-stage-dropped-from-dvc-yaml", "dvc-yaml-removed"])
def test_staged_finds_an_id_moved_out_of_an_unchanged_lock(tmp_path: Path, shape: str) -> None:
    """An out leaves a stage the commit no longer declares (or a dvc.yaml
    it removes) for a new `.dvc` file with the same bytes and no ID, while
    dvc.lock itself is unchanged: HEAD's lock is read whenever a pointer
    changes, so the dropped ID is found.

    Mutation: read HEAD's lock only when the commit changes it -> [], red.
    """
    proj = _repo(tmp_path)
    md5 = _md5(b"x\n")
    (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - data/x.csv\n")
    (proj / "dvc.lock").write_text(_lock("build", md5, "vx"))
    _commit(proj)
    if shape == "its-stage-dropped-from-dvc-yaml":
        (proj / "dvc.yaml").write_text("stages: {}\n")
    else:
        _git(proj, "rm", "-q", "dvc.yaml")
    (proj / "data").mkdir(exist_ok=True)
    (proj / "data" / "x.csv.dvc").write_text(_file_pointer(md5, None, path="x.csv"))
    _git(proj, "add", "-A")
    assert [f.message for f in check_staged(proj)] == [
        "data/x.csv.dvc drops S3 version IDs from 1 unchanged file(s) (data/x.csv)"]


@pytest.mark.parametrize("shape", ["a-file", "a-bare-tree", "a-bare-tree-and-the-other-deleted"])
def test_staged_lets_through_a_key_another_staged_pointer_still_pins(tmp_path: Path, shape: str) -> None:
    """Two pointers record one key, one with its ID: a commit touching both
    drops no ID, since the staged commit still records it for those bytes,
    whether the other records the file with no ID or as part of a folder
    out written back as a bare tree hash. When the one with the ID goes
    too, the bare tree still matches HEAD's files, each counted once, and
    the drop is refused.

    Mutations: refuse an entry with no ID whatever the others record ->
    a-file red; the same for a bare tree's files -> a-bare-tree red; count
    HEAD's files once per pointer -> the tree doesn't match and the last
    case passes, red.
    """
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    proj = _repo(tmp_path)
    md5 = _md5(b"x\n")
    (proj / "data").mkdir()
    if shape == "a-file":
        (proj / "data" / "x.csv.dvc").write_text(_file_pointer(md5, "vx", path="x.csv"))
        (proj / "data" / "copy.dvc").write_text(_file_pointer(md5, None, path="x.csv"))  # same key, never an ID
        _commit(proj)
        for rel, vid in (("data/copy.dvc", None), ("data/x.csv.dvc", "vx")):
            (proj / rel).write_text(_file_pointer(md5, vid, path="x.csv") + "meta:\n  note: hi\n")
    else:
        (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - data/out\n")
        lock = ["schema: '2.0'", "stages:", "  build:", "    cmd: run", "    outs:", "    - path: data/out",
                "      hash: md5", "      files:", "      - relpath: a.csv", f"        md5: {md5}", "        size: 2",
                "        cloud:", f"          {REMOTE}:", f"            etag: {md5}", "            version_id: va"]
        (proj / "dvc.lock").write_text("\n".join(lock) + "\n")
        _folder_pointer(proj, "out", {"a.csv": (md5, "va")})  # data/out.dvc: the same key, with its ID
        _commit(proj)
        tree = dir_manifest_md5([DvcFileEntry(md5=md5, relpath="a.csv", size=2)])
        (proj / "dvc.lock").write_text("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n"
                                       f"    - path: data/out\n      hash: md5\n      md5: {tree}\n      size: 2\n"
                                       "      nfiles: 1\n")
        if shape == "a-bare-tree":
            (proj / "data" / "out.dvc").write_text((proj / "data" / "out.dvc").read_text() + "meta:\n  note: hi\n")
        else:
            _git(proj, "rm", "-q", "data/out.dvc")
    _git(proj, "add", "-A")
    if shape == "a-bare-tree-and-the-other-deleted":
        assert [f.message for f in check_staged(proj)] == [
            "dvc.lock drops S3 version IDs from 1 unchanged file(s) (data/out/a.csv)"]
    else:
        assert check_staged(proj) == []


@pytest.mark.parametrize("remote", ["not-version-aware", "an-out-s-own"])
def test_staged_reads_the_remote_as_push_does(tmp_path: Path, remote: str) -> None:
    """`pinned_remote`, push's own test: a remote that isn't version-aware
    pins nothing, so nothing can be dropped and push can't put IDs back
    there; an out with its own remote is one push refuses, so it is named.

    Mutations: skip the version-aware test -> a false refusal whose hint is
    push, red; skip the own-remote refusal -> [], red.
    """
    proj = _repo(tmp_path, version_aware=remote == "an-out-s-own")
    md5 = _md5(b"x\n")
    own = remote == "an-out-s-own"
    (proj / "data").mkdir()
    named = {"remote": "other", "own_remote": True} if own else {}
    (proj / "data" / "x.csv.dvc").write_text(_file_pointer(md5, "vx", path="x.csv", **named))
    _commit(proj)
    (proj / "data" / "x.csv.dvc").write_text(_file_pointer(md5, None, path="x.csv", **named))
    _git(proj, "add", "-A")
    if not own:
        assert check_staged(proj) == []
    else:
        (finding,) = check_staged(proj)
        assert (finding.kind, finding.message) == (
            "staged_unchecked", "can't check the staged commit: outputs with their own remote aren't supported on a "
            "version-aware remote: data/x.csv.dvc: x.csv")


@pytest.mark.parametrize("head", ["a-conflicted-lock", "an-impossible-date", "fixed-with-its-ids"])
def test_staged_names_a_head_copy_it_cant_read(tmp_path: Path, head: str) -> None:
    """HEAD's copy read as "no outs" would hide every ID it records, so a
    commit that records a file with no ID over a HEAD copy dvc couldn't
    read is named (here, what `dvc commit -f` writes over a lock a merge
    left conflicted, or over a pointer with an impossible date). A commit
    that records every file's ID drops none, whatever HEAD's copy is.

    Mutations: skip HEAD's screen -> the first two pass, red; name it
    whatever the commit records -> fixed-with-its-ids refused, red.
    """
    md5 = _md5(b"x\n")
    proj = _repo(tmp_path)
    (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - data/x.csv\n")
    good = _lock("build", md5, "vx")
    (proj / "dvc.lock").write_text(f"<<<<<<< ours\n{good}=======\n{good}>>>>>>> theirs\n" if head == "a-conflicted-lock"
                                   else f"{good}meta:\n  reviewed: 2023-02-29\n")
    _commit(proj)
    (proj / "dvc.lock").write_text(_lock("build", md5, "vx" if head == "fixed-with-its-ids" else None))
    _git(proj, "add", "-A")
    got = check_staged(proj)
    if head == "fixed-with-its-ids":
        assert got == []
    else:
        (finding,) = got
        assert (finding.severity, finding.kind) == ("error", "staged_unchecked")
        assert finding.message.startswith("can't check the staged commit: HEAD's dvc.lock can't be read (")
        assert finding.message.endswith("), so the IDs it records can't be compared")


@pytest.mark.parametrize("head", ["a-conflicted-pointer", "a-conflicted-lock-under-a-bare-tree",
                                  "a-broken-dvc-yaml-and-no-lock-in-the-commit",
                                  "a-broken-dvc-yaml-and-the-first-lock"])
def test_staged_screens_the_head_copies_its_comparison_reads(tmp_path: Path, head: str) -> None:
    """HEAD's screen covers what the comparison reads: a changed `.dvc`
    file's HEAD copy, and HEAD's lock under a folder out `dvc commit -f`
    rewrote as a bare tree hash. HEAD's dvc.yaml is read only beside a lock
    HEAD has, so a broken one the commit fixes refuses nothing, whether the
    commit has no lock or adds the first one (a first `dvc repro`, not
    pushed yet).

    Mutations: screen no `.dvc` file at HEAD -> a-conflicted-pointer passes,
    red; leave a bare tree out of "the commit records a file with no ID" ->
    the bare-tree case passes, red; write HEAD's dvc.yaml into every HEAD
    tree -> both dvc.yaml cases refused, red; write it whenever the commit
    changes dvc.lock -> the first-lock case refused, red.
    """
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    def conflicted(text: str) -> str:
        return f"<<<<<<< ours\n{text}=======\n{text}>>>>>>> theirs\n"

    proj = _repo(tmp_path)
    files = {"a.csv": _md5(b"a\n"), "b.csv": _md5(b"b\n")}
    if head == "a-conflicted-pointer":
        _folder_pointer(proj, "final", {rel: (md5, f"v{rel}") for rel, md5 in files.items()})
        (proj / "data" / "final.dvc").write_text(conflicted((proj / "data" / "final.dvc").read_text()))
        _commit(proj)
        _folder_pointer(proj, "final", {rel: (md5, None) for rel, md5 in files.items()})
        want = "data/final.dvc"
    elif head == "a-conflicted-lock-under-a-bare-tree":
        (proj / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - data/out\n")
        lock = ["schema: '2.0'", "stages:", "  build:", "    cmd: run", "    outs:", "    - path: data/out",
                "      hash: md5", "      files:"]
        for rel, md5 in files.items():
            lock += [f"      - relpath: {rel}", f"        md5: {md5}", "        size: 2", "        cloud:",
                     f"          {REMOTE}:", f"            etag: {md5}", f"            version_id: v{rel}"]
        (proj / "dvc.lock").write_text(conflicted("\n".join(lock) + "\n"))
        _commit(proj)
        tree = dir_manifest_md5([DvcFileEntry(md5=m, relpath=r, size=2) for r, m in files.items()])
        (proj / "dvc.lock").write_text("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n"
                                       f"    - path: data/out\n      hash: md5\n      md5: {tree}\n      size: 4\n"
                                       "      nfiles: 2\n")
        want = "dvc.lock"
    elif head == "a-broken-dvc-yaml-and-no-lock-in-the-commit":
        (proj / "dvc.yaml").write_text(conflicted("stages: {}\n"))
        _folder_pointer(proj, "a", {"x.csv": (files["a.csv"], "va")})
        _commit(proj)
        (proj / "dvc.yaml").write_text("stages: {}\n")
        _folder_pointer(proj, "b", {"x.csv": (files["b.csv"], None)})  # new data, not pushed yet
        want = None
    else:
        stage = "stages:\n  build:\n    cmd: run\n    outs:\n    - data/x.csv\n"
        (proj / "dvc.yaml").write_text(conflicted(stage))
        _folder_pointer(proj, "a", {"x.csv": (files["a.csv"], "va")})
        _commit(proj)
        (proj / "dvc.yaml").write_text(stage)
        (proj / "dvc.lock").write_text(_lock("build", _md5(b"x\n"), None))  # the first lock, not pushed yet
        want = None
    _git(proj, "add", "-A")
    got = check_staged(proj)
    if want is None:
        assert got == []
    else:
        assert [f.message.split(" can't be read")[0] for f in got] == [f"can't check the staged commit: HEAD's {want}"]


@pytest.mark.parametrize("setting", ['version_aware = "true"', "worktree = true"])
def test_staged_reads_version_aware_as_dvc_does(tmp_path: Path, setting: str) -> None:
    """A quoted `version_aware`, or `worktree = true` with no
    `version_aware`, is a version-aware remote to dvc, which records IDs
    there: a commit that drops them is refused.

    Mutation: read only an unquoted `version_aware` key -> [], red.
    """
    proj, files = _pinned_repo(tmp_path)
    cfg = proj / ".dvc" / "config"
    cfg.write_text(cfg.read_text().replace("version_aware = true", setting))
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "-A")
    assert [f.kind for f in check_staged(proj)] == ["staged_drops_version_ids"]


@pytest.mark.parametrize("shape", ["staged-dvc-yaml", "deleted-pointer", "remote-working-tree-dvc-yaml",
                                   "staged-on-a-remote-that-isnt-version-aware"])
def test_an_impossible_date_is_read_as_unparseable_not_a_traceback(tmp_path: Path, s3, shape: str) -> None:
    """ruamel raises a bare ValueError for `2023-02-29`. Every reader takes
    the file as unparseable: the screen names it where it's read (a staged
    dvc.yaml, the working tree's), and a pointer the commit deletes drops
    nothing. The screen runs before the remote is read, so a damaged staged
    pointer is named even on a remote that pins nothing.

    Mutations: read12 catches YAMLError only -> ValueError, red; screen
    after pinned_remote -> the not-version-aware case passes, red.
    """
    bad = "meta:\n  reviewed: 2023-02-29\n"
    if shape == "remote-working-tree-dvc-yaml":
        proj, *_ = _pushed(tmp_path, s3)
        (proj / "dvc.yaml").write_text(f"stages: {{}}\n{bad}")
        kinds = [f.kind for f in check_project(proj, remote=True)]
        assert "dvc_state_unreadable" in kinds
        return
    proj, files = _pinned_repo(tmp_path)
    if shape == "staged-dvc-yaml":  # with a pointer that drops its IDs, so the trees are read
        (proj / "dvc.yaml").write_text(f"stages: {{}}\n{bad}")
        _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
        want = [("dvc_state_unreadable", "could not read dvc.yaml in the staged commit: ")]
    elif shape == "deleted-pointer":
        (proj / "data" / "bad.dvc").write_text(_file_pointer(_md5(b"x\n"), "vx", path="bad") + bad)
        _commit(proj)
        _git(proj, "rm", "-q", "data/bad.dvc")
        want = []
    else:
        cfg = proj / ".dvc" / "config"
        cfg.write_text(cfg.read_text().replace("    version_aware = true\n", ""))
        (proj / "data" / "final.dvc").write_text((proj / "data" / "final.dvc").read_text() + bad)
        want = [("dvc_state_unreadable", "could not read data/final.dvc in the staged commit: ")]
    _git(proj, "add", "-A")
    got = check_staged(proj)
    assert len(got) == len(want)
    assert [(f.kind, f.message[:len(m)]) for f, (_, m) in zip(got, want, strict=True)] == want


@pytest.mark.parametrize("damage", ["conflict-markers", "cut-short", "not-utf-8", "duplicate-key",
                                    "impossible-date"])
def test_staged_refuses_a_pointer_it_cant_read(tmp_path: Path, damage: str) -> None:
    """A staged pointer dvc would refuse is not "no outs": read that way, it
    would look like a pointer dropped on purpose and pass.

    Mutations: skip the screen on the staged tree -> [], red; write the
    scratch tree without surrogateescape -> UnicodeEncodeError, red.
    """
    proj, files = _pinned_repo(tmp_path)
    good = (proj / "data" / "final.dvc").read_text()
    (proj / "data" / "final.dvc").write_bytes(_damaged(good, damage))
    _git(proj, "add", "data/final.dvc")
    (finding,) = check_staged(proj)
    assert (finding.severity, finding.kind) == ("error", "dvc_state_unreadable")
    assert finding.message.startswith("could not read data/final.dvc in the staged commit: ")
    if damage == "not-utf-8":
        assert finding.message.endswith(": not UTF-8 text (dvc writes UTF-8)")


def _stage_a_dvc_commit_f_on_a_wdir_stage(proj: Path) -> None:
    """What `dvc commit -f <stage>` stages for a `wdir: code` stage's folder
    out: dvc.lock's out written back as a bare tree hash, with no file list
    and no IDs, over a commit that recorded each file's ID."""
    from mintd._dvc_state import DvcFileEntry, dir_manifest_md5

    (proj / "code").mkdir()
    (proj / "code" / "run.py").write_text("print('run')\n")
    (proj / "dvc.yaml").write_text("stages:\n  build:\n    wdir: code\n    cmd: run\n    outs:\n    - ../data/out\n")
    files = {"a.csv": _md5(b"a\n"), "b.csv": _md5(b"b\n")}
    head = ["schema: '2.0'", "stages:", "  build:", "    cmd: run", "    outs:", "    - path: ../data/out",
            "      hash: md5", "      files:"]
    for rel, md5 in files.items():
        head += [f"      - relpath: {rel}", f"        md5: {md5}", "        size: 2", "        cloud:",
                 f"          {REMOTE}:", f"            etag: {md5}", f"            version_id: v{rel}"]
    (proj / "dvc.lock").write_text("\n".join(head) + "\n")
    _commit(proj)
    tree = dir_manifest_md5([DvcFileEntry(md5=m, relpath=r, size=2) for r, m in files.items()])
    (proj / "dvc.lock").write_text("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: ../data/out\n"
                                   f"      hash: md5\n      md5: {tree}\n      size: 4\n      nfiles: 2\n")
    _git(proj, "add", "dvc.lock")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_staged_from_a_project_subfolder_checks_the_project(tmp_path: Path, monkeypatch, layout: str) -> None:
    """The project is found as dvc finds it, up from the path: run from
    `proj/code` (the CLI's path defaults to "."), it checks `proj`, and
    refuses what `dvc commit -f` stages for a `wdir: code` stage.

    Mutation: check only the folder given -> no .dvc/ there, nothing found,
    red.
    """
    proj = _repo(tmp_path, layout=layout)
    _stage_a_dvc_commit_f_on_a_wdir_stage(proj)
    want = ["dvc.lock drops S3 version IDs from 2 unchanged file(s) (data/out/a.csv, data/out/b.csv)"]
    assert [f.message for f in check_staged(proj / "code")] == want
    assert cli.main(["check", "--staged", str(proj / "code")]) == 1
    monkeypatch.chdir(proj / "code")
    assert cli.main(["check", "--staged"]) == 1


@pytest.mark.parametrize("where", ["the-repo-root-above", "a-sibling-folder"])
def test_staged_outside_any_dvc_project_refuses_a_commit_that_stages_pointers(tmp_path: Path, where: str) -> None:
    """No DVC project up from the path (the repository's root above one, a
    folder beside one): the whole commit's staged list is read, and a staged
    pointer means the check belongs in its project.

    Mutation: read only what's staged under the path -> a-sibling-folder
    sees nothing and passes, red.
    """
    proj = _repo(tmp_path, layout="below-the-repo-root")
    (proj.parent / "docs").mkdir()
    (proj.parent / "docs" / "notes.md").write_text("hi\n")
    _stage_a_dvc_commit_f_on_a_wdir_stage(proj)
    path = proj.parent if where == "the-repo-root-above" else proj.parent / "docs"
    (finding,) = check_staged(path)
    assert (finding.severity, finding.kind) == ("error", "staged_unchecked")
    assert finding.message == (f"can't check the staged commit: {path} isn't in a DVC project, and the commit "
                               "stages DVC pointers (proj/dvc.lock)")
    assert cli.main(["check", "--staged", str(path)]) == 1


@pytest.mark.parametrize("where", ["missing", "outside-git"])
def test_staged_refuses_a_missing_path_or_one_outside_git_before_any_git_call(
    tmp_path: Path, monkeypatch, where: str,
) -> None:
    """With git off PATH, a git call would raise; the wrong folder is said
    first.

    Mutations: drop the folder test -> missing reads as outside git, red;
    take the path as the repository's top -> git is asked and raises, red.
    """
    path = tmp_path / ("nope" if where == "missing" else "plain")
    if where == "outside-git":
        path.mkdir()
    _git_fails(tmp_path, monkeypatch, "missing")
    (finding,) = check_staged(path)
    assert (finding.severity, finding.kind) == ("error", "staged_unchecked")
    why = "isn't a folder" if where == "missing" else "isn't in a git repository"
    assert finding.message == f"can't check the staged commit: {path} {why}"
    assert cli.main(["check", "--staged", str(path)]) == 1


def test_staged_in_a_nested_git_repo_stops_at_its_own_top_level(tmp_path: Path) -> None:
    """A git repository inside a DVC project's folder is its own: the walk up
    stops at its top level, and its commit stages nothing.

    Mutation: walk up past the git top level -> the outer project's staged
    drop is reported for the inner repository, red.
    """
    proj, files = _pinned_repo(tmp_path)
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "data/final.dvc")
    inner = proj / "vendor"
    inner.mkdir()
    _git(inner, "init", "-q", "-b", "main")
    assert check_staged(inner) == []
    assert [f.kind for f in check_staged(proj)] == ["staged_drops_version_ids"]  # the outer one still refuses


def test_dvc_root_stops_at_its_bound(tmp_path: Path) -> None:
    """dvc_root walks up as dvc does, past any .git; with `within`, never above it.
    Mutation: ignore `within` -> the outer project is found, red."""
    from mintd._dvc_invoke import dvc_root

    (tmp_path / ".dvc").mkdir()
    inner = tmp_path / "repo" / "sub"
    inner.mkdir(parents=True)
    assert dvc_root(inner) == tmp_path
    assert dvc_root(inner, within=tmp_path / "repo") == inner  # none within: `start`, as with no project at all
    (tmp_path / "repo" / ".dvc").mkdir()
    assert dvc_root(inner, within=tmp_path / "repo") == tmp_path / "repo"


def test_staged_checks_the_nearest_dvc_project_up_from_the_path(tmp_path: Path) -> None:
    """A project inside another's folder, in the same git repository, is the
    one dvc runs in from below it, so it is the one checked.

    Mutation: take the outermost `.dvc/` up from the path -> the finding
    names `inner/data/final.dvc`, red.
    """
    outer = _repo(tmp_path)
    inner = outer / "inner"
    inner.mkdir()
    stage_dvc_project(inner)
    files = {"a.csv": (_md5(b"a\n"), "va")}
    _folder_pointer(inner, "final", files)
    _commit(outer)
    _folder_pointer(inner, "final", {"a.csv": (files["a.csv"][0], None)})
    _git(outer, "add", "-A")
    (finding,) = check_staged(inner / "data")
    assert finding.message == "data/final.dvc drops S3 version IDs from 1 unchanged file(s) (data/final/a.csv)"
    assert finding.source == inner / "data" / "final.dvc"


def test_staged_passes_a_repo_with_no_dvc_folder_and_no_pointer_staged(tmp_path: Path) -> None:
    """An enclave repo has no .dvc/ by design: with no pointer staged there
    is nothing to check, not an error.

    Mutation: refuse every path without .dvc/ -> red.
    """
    repo = tmp_path / "enclave"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    assert check_staged(repo) == []
    assert cli.main(["check", "--staged", str(repo)]) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't name a file with a quote")
def test_staged_reads_a_pointer_whose_name_git_would_quote(tmp_path: Path) -> None:
    """Mutation: list staged paths without -z -> `"data/we\\"ird.dvc"`
    doesn't end in .dvc and is skipped, red."""
    proj = _repo(tmp_path)
    files = {"a.csv": (_md5(b"a\n"), "va")}
    _folder_pointer(proj, 'we"ird', files)
    _commit(proj)
    _folder_pointer(proj, 'we"ird', {"a.csv": (files["a.csv"][0], None)})
    _git(proj, "add", "-A")
    (finding,) = check_staged(proj)
    assert finding.message == 'data/we"ird.dvc drops S3 version IDs from 1 unchanged file(s) (data/we"ird/a.csv)'


@pytest.mark.parametrize("ref", ["garbage", "empty", "missing-object"])
def test_a_broken_head_is_not_read_as_no_commit_yet(tmp_path: Path, s3, ref: str) -> None:
    """Git answers an unborn branch and a broken HEAD ref alike to
    `rev-parse --verify --quiet`; only a branch with no ref at all is "no
    commit yet". A broken one fails both checks.

    Mutation: read any quiet rev-parse failure as no commit -> [] for
    --staged, HEAD skipped for --remote, red.
    """
    proj, files = _pinned_repo(tmp_path)
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "data/final.dvc")
    content = {"garbage": "garbage\n", "empty": "", "missing-object": "1234567890" * 4 + "\n"}[ref]
    (proj / ".git" / "refs" / "heads" / "main").write_text(content)
    with pytest.raises(GitOpError):
        check_staged(proj)
    with pytest.raises(GitOpError):
        check_project(proj, remote=True)


def test_check_staged_exits_1_on_a_dropped_id(tmp_path: Path) -> None:
    proj, files = _pinned_repo(tmp_path)
    _folder_pointer(proj, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
    _git(proj, "add", "data/final.dvc")
    assert cli.main(["check", "--staged", str(proj)]) == 1


# --- the offline dvc.lock dep rule -----------------------------------------

@pytest.mark.parametrize("dep, warned", [
    ("    - path: ../data/raw\n      hash: md5\n", True),
    ("    - path: ../data/raw\n      hash: md5\n      md5: 0123456789abcdef0123456789abcdef.dir\n", False),
    ("    - path: s3://bucket/raw.csv\n      hash: md5\n      etag: abc\n      size: 5\n", False),
], ids=["no-hash", "hashed", "out-of-repo"])
def test_check_warns_of_a_lock_dep_recorded_with_no_hash(tmp_path: Path, dep: str, warned: bool) -> None:
    """An out-of-repo dep records its remote's checksum (etag on S3), as DVC
    dumps it, under `hash: md5`: that is a hash.

    Mutations: drop the rule -> the no-hash case has no warning, red; read
    only md5 -> the out-of-repo case is warned, red.
    """
    proj = _repo(tmp_path)
    (proj / "dvc.yaml").write_text("stages:\n  build:\n    wdir: code\n    cmd: run\n    deps:\n    - ../data/raw\n")
    (proj / "dvc.lock").write_text("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    deps:\n" + dep)
    found = [f for f in check_project(proj) if f.kind == "dvc_lock_dep_unhashed"]
    assert [(f.severity, f.message) for f in found] == (
        [("warning", "dvc.lock stage build records its dep ../data/raw with no hash: the lock was re-dumped "
                     "without the dep's tree")] if warned else [])
