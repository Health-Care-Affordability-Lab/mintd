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


def test_remote_leaves_out_a_committed_pointer_gitignore_excludes(pushed) -> None:
    """DVC, and so push and importers, skip a `.dvc` file `.gitignore`
    excludes, even one force-added; HEAD's check skips it too.

    Mutation: read every committed pointer at HEAD -> its stale pin is
    reported, red.
    """
    from mintd._fast_sync_ops import gitignored_pointers

    proj, _, _, files = pushed
    (proj / "scratch").mkdir()
    (proj / "scratch" / "old.dvc").write_text(
        f"outs:\n- hash: md5\n  path: old\n  files:\n  - relpath: a.csv\n    md5: {files['a.csv'][0]}\n"
        f"    size: 2\n    cloud:\n      {REMOTE}:\n        etag: {files['a.csv'][0]}\n"
        f"        version_id: {files['b.csv'][1]}\n")
    with (proj / ".gitignore").open("a") as f:
        f.write("/scratch/\n")
    _git(proj, "add", "-f", "scratch/old.dvc")
    _commit(proj)
    assert gitignored_pointers(proj) == ["scratch/old.dvc"]
    assert _remote(check_project(proj, remote=True, all_outs=True)) == []


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
    from mintd._fast_sync_ops import DvcFileEntry, dir_manifest_md5

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
    assert "records no S3 version ID, so importers of that commit can't fetch it" in finding.message
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
    from mintd._fast_sync_ops import DvcFileEntry, dir_manifest_md5

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


def test_remote_names_a_pointer_it_cant_read_at_head(pushed) -> None:
    """A merge left conflict markers in HEAD's pointer and the working tree
    was fixed but not committed: importers read the damaged one.

    Mutation: skip the screen at HEAD -> read as no outs, nothing, red.
    """
    proj, *_ = pushed
    good = (proj / "data" / "final.dvc").read_text()
    (proj / "data" / "final.dvc").write_text(f"<<<<<<< ours\n{good}=======\n{good}>>>>>>> theirs\n")
    _commit(proj)
    (proj / "data" / "final.dvc").write_text(good)
    (finding,) = [f for f in check_project(proj, remote=True) if f.kind == "dvc_state_unreadable"]
    assert finding.severity == "error" and finding.message.startswith("could not read data/final.dvc at HEAD: ")


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


@pytest.mark.parametrize("change", ["new-data", "ids-added", "file-removed", "new-pointer", "pointer-deleted",
                                    "readme", "another-project"])
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
    else:  # what dvc commit -f does, in the other project
        _folder_pointer(other, "final", {rel: (md5, None) for rel, (md5, _) in files.items()})
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
    from mintd._fast_sync_ops import DvcFileEntry, dir_manifest_md5

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


@pytest.mark.parametrize("damage", ["conflict-markers", "cut-short"])
def test_staged_refuses_a_pointer_it_cant_read(tmp_path: Path, damage: str) -> None:
    """A staged pointer dvc would refuse is not "no outs": read that way, it
    would look like a pointer dropped on purpose and pass.

    Mutation: skip the screen on the staged tree -> [], red.
    """
    proj, files = _pinned_repo(tmp_path)
    good = (proj / "data" / "final.dvc").read_text()
    damaged = (f"<<<<<<< ours\n{good}=======\n{good}>>>>>>> theirs\n" if damage == "conflict-markers"
               else good[:good.index("  path:")])  # cut before the out's path: the cut-short rule's case
    (proj / "data" / "final.dvc").write_text(damaged)
    _git(proj, "add", "data/final.dvc")
    (finding,) = check_staged(proj)
    assert (finding.severity, finding.kind) == ("error", "dvc_state_unreadable")
    assert finding.message.startswith("could not read data/final.dvc in the staged commit: ")


def _stage_the_cms_pdc_commit(proj: Path) -> None:
    """What `dvc commit -f <stage>` stages for a `wdir: code` stage's folder
    out: dvc.lock's out written back as a bare tree hash, with no file list
    and no IDs, over a commit that recorded each file's ID."""
    from mintd._fast_sync_ops import DvcFileEntry, dir_manifest_md5

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
    refuses the cms-pdc commit.

    Mutation: check only the folder given -> no .dvc/ there, nothing found,
    red.
    """
    proj = _repo(tmp_path, layout=layout)
    _stage_the_cms_pdc_commit(proj)
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
    _stage_the_cms_pdc_commit(proj)
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
