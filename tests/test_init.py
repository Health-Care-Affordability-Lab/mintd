from __future__ import annotations

import json
from pathlib import Path

import pytest
from mintd._dvc_invoke import dvc_cmd

from mintd._console import Reporter
from mintd._init_ops import InitOpError
from mintd.init import init_project, InitDestinationExists, InitNameInvalid
from mintd.model import Metadata
from tests._fakes.init_ops import _FakeInitOps


def test_init_default_creates_typed_subdir(tmp_path: Path) -> None:
    """Default mode scaffolds into ``target_dir/{type}_{name}``."""
    fake = _FakeInitOps()
    project_path, written = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    assert project_path == tmp_path / "data_my_proj"
    assert (tmp_path / "data_my_proj" / "metadata.json").exists()
    assert (tmp_path / "data_my_proj" / ".gitignore").exists()
    assert len(written) > 5  # rich scaffold; more than just metadata + gitignore


def test_init_use_current_repo_writes_into_target_dir(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data",
        name="my_proj",
        target_dir=tmp_path,
        use_current_repo=True,
        ops=fake,
    )
    assert project_path == tmp_path
    assert (tmp_path / "metadata.json").exists()
    assert not (tmp_path / "data_my_proj").exists()


def test_init_writes_metadata_json(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    metadata_path = project_path / "metadata.json"
    assert metadata_path.exists()
    Metadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))


def test_init_writes_gitignore(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    gitignore_path = project_path / ".gitignore"
    assert gitignore_path.exists()
    # The vendored .gitignore is the legacy one; just confirm non-empty.
    assert gitignore_path.read_text(encoding="utf-8").strip()


def test_init_runs_git_init(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    assert fake.git_calls == [project_path]


@pytest.mark.parametrize("ptype", ["data", "code", "project"])
def test_init_runs_dvc_init_for_valid_types(tmp_path: Path, ptype: str) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type=ptype, name="my_proj", target_dir=tmp_path, ops=fake
    )
    assert fake.dvc_calls == [project_path]


def test_init_skips_dvc_init_for_enclave_type(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="enclave", name="my_proj", target_dir=tmp_path, ops=fake
    )
    assert fake.dvc_calls == []
    assert fake.git_calls == [project_path]


def test_init_existing_metadata_raises(tmp_path: Path) -> None:
    project_path = tmp_path / "data_my_proj"
    project_path.mkdir()
    metadata_path = project_path / "metadata.json"
    metadata_path.write_text("{}")

    fake = _FakeInitOps()
    with pytest.raises(InitDestinationExists):
        init_project(
            project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
        )
    assert metadata_path.read_text(encoding="utf-8") == "{}"
    assert fake.git_calls == []
    assert fake.dvc_calls == []


def test_init_refuses_to_overwrite_scaffold_files(tmp_path: Path) -> None:
    """Files the user wrote by hand are not the scaffold's to replace.

    At HEAD the only guarded path was metadata.json, so a hand-written
    README.md was silently replaced and printed back as `created:`.
    """
    project_path = tmp_path / "data_my_proj"
    project_path.mkdir()
    (project_path / "README.md").write_text("MY NOTES\n", encoding="utf-8")
    (project_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")

    fake = _FakeInitOps()
    with pytest.raises(InitDestinationExists) as excinfo:
        init_project(
            project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
        )

    msg = str(excinfo.value)
    assert "README.md" in msg and ".gitignore" in msg
    assert (project_path / "README.md").read_text(encoding="utf-8") == "MY NOTES\n"
    assert (project_path / ".gitignore").read_text(encoding="utf-8") == "*.secret\n"
    assert fake.git_calls == []
    assert fake.dvc_calls == []


def test_init_use_current_repo_tolerates_non_scaffold_files(tmp_path: Path) -> None:
    """The negative control: refusing on *any* file would break the flow
    `--use-current-repo` exists for. Only scaffold-owned paths collide."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep me\n", encoding="utf-8")

    fake = _FakeInitOps()
    project_path, written = init_project(
        project_type="data",
        name="my_proj",
        target_dir=tmp_path,
        use_current_repo=True,
        ops=fake,
    )

    assert (project_path / "metadata.json").exists()
    assert written
    assert (tmp_path / "LICENSE").read_text(encoding="utf-8") == "MIT\n"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "keep me\n"


def test_init_force_overwrites_colliding_files(tmp_path: Path) -> None:
    """`--force` is the documented escape hatch and must actually overwrite."""
    project_path = tmp_path / "data_my_proj"
    project_path.mkdir()
    (project_path / "README.md").write_text("MY NOTES\n", encoding="utf-8")

    fake = _FakeInitOps()
    init_project(
        project_type="data",
        name="my_proj",
        target_dir=tmp_path,
        force=True,
        ops=fake,
    )
    assert (project_path / "README.md").read_text(encoding="utf-8") != "MY NOTES\n"


def test_init_refuses_to_write_outside_the_project_through_a_symlink(
    tmp_path: Path,
) -> None:
    """Not even --force may follow a link out of the project.

    --force authorizes overwriting files in THIS project. Two shapes: a
    symlinked leaf (README.md -> ../shared/TEAM-README.md) and a symlinked
    directory (code/ -> ../shared_code), which os.path.lexists cannot see
    because mkdir is satisfied by the link and write_text follows it.
    """
    outside = tmp_path / "shared"
    outside.mkdir()
    team_doc = outside / "TEAM-README.md"
    team_doc.write_text("TEAM DOC\n", encoding="utf-8")

    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").symlink_to(team_doc)

    for force in (False, True):
        with pytest.raises(InitDestinationExists):
            init_project(
                project_type="data",
                name="cohort",
                target_dir=work,
                use_current_repo=True,
                force=force,
                ops=_FakeInitOps(),
            )
    assert team_doc.read_text(encoding="utf-8") == "TEAM DOC\n"


def test_init_refuses_a_symlinked_scaffold_directory(tmp_path: Path) -> None:
    """The parent-directory case the leaf check cannot see."""
    shared_code = tmp_path / "shared_code"
    shared_code.mkdir()
    team_utils = shared_code / "_mintd_utils.py"
    team_utils.write_text("TEAM UTILS\n", encoding="utf-8")

    work = tmp_path / "work"
    work.mkdir()
    (work / "code").symlink_to(shared_code, target_is_directory=True)

    with pytest.raises(InitDestinationExists, match="outside"):
        init_project(
            project_type="data",
            name="cohort",
            target_dir=work,
            use_current_repo=True,
            force=True,
            ops=_FakeInitOps(),
        )
    assert team_utils.read_text(encoding="utf-8") == "TEAM UTILS\n"
    assert list(shared_code.iterdir()) == [team_utils]


def test_init_code_type_ignores_a_readme(tmp_path: Path) -> None:
    """A code scaffold writes metadata.json and nothing else, so a README in
    the target is not a collision for it."""
    project_path = tmp_path / "my_proj"
    project_path.mkdir()
    (project_path / "README.md").write_text("MY NOTES\n", encoding="utf-8")

    fake = _FakeInitOps()
    init_project(
        project_type="code", name="my_proj", target_dir=tmp_path, ops=fake
    )
    assert (project_path / "README.md").read_text(encoding="utf-8") == "MY NOTES\n"
    assert (project_path / "metadata.json").exists()


def test_init_missing_bucket_writes_nothing(tmp_path: Path) -> None:
    """The likeliest first-run failure must leave no half-made project.

    At HEAD this raised after the scaffold, `git init` and `dvc init` had
    run, and *outside* the rollback boundary -- so metadata.json and .dvc/
    survived and the rerun wedged on the destination guard.
    """
    fake = _FakeInitOps()
    with pytest.raises(InitOpError, match="bucket not configured"):
        init_project(
            project_type="data",
            name="my_proj",
            target_dir=tmp_path,
            classification="labonly",
            bucket=None,
            endpoint="",
            ops=fake,
        )
    assert not (tmp_path / "data_my_proj").exists()
    assert fake.git_calls == []
    assert fake.dvc_calls == []


def test_init_invalid_name_leaves_no_directory(tmp_path: Path) -> None:
    """Validation runs before mkdir, so a bad name leaves no stray dir."""
    fake = _FakeInitOps()
    with pytest.raises(InitNameInvalid):
        init_project(
            project_type="data", name="-bad", target_dir=tmp_path, ops=fake
        )
    assert list(tmp_path.iterdir()) == []


def test_init_creates_target_dir_if_missing(tmp_path: Path) -> None:
    target_dir = tmp_path / "new" / "nested"
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=target_dir, ops=fake
    )
    assert project_path.exists()
    assert project_path == target_dir / "data_my_proj"


def test_init_metadata_includes_passed_name_and_type(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    metadata = Metadata.model_validate_json(
        (project_path / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata.project.name == "my_proj"
    assert metadata.project.type == "data"
    assert metadata.project.full_name == "data_my_proj"


def test_init_python_data_writes_rich_scaffold(tmp_path: Path) -> None:
    """Slice-19 acceptance: rich scaffold lands by default for python data."""
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    assert (project_path / "README.md").exists()
    assert (project_path / "requirements.txt").exists()
    assert (project_path / "code" / "ingest.py").exists()
    # The clean.* stub was deleted (demoted into ingest's parse_and_clean).
    assert not (project_path / "code" / "clean.py").exists()
    assert (project_path / "code" / "validate.py").exists()
    # Slice 41: scaffold no longer ships generate_schema.py.
    assert not (project_path / "schemas" / "generate_schema.py").exists()


def test_init_invalid_name_raises(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    with pytest.raises(InitNameInvalid):
        init_project(
            project_type="data", name="-bad", target_dir=tmp_path, ops=fake
        )


# ---------------------------------------------------------------------------
# Slice 30 — init redesign: classification + storage block + remote add
# ---------------------------------------------------------------------------

def _read_metadata(project_path: Path) -> Metadata:
    return Metadata.model_validate_json(
        (project_path / "metadata.json").read_text(encoding="utf-8")
    )


def test_init_project_writes_full_storage_block(tmp_path: Path) -> None:
    """labonly init writes a complete Storage with all six required fields."""
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="https://s3.wasabisys.com",
        ops=fake,
    )
    m = _read_metadata(project_path)
    assert m.storage is not None
    assert m.storage.provider == "s3"
    assert m.storage.bucket == "cooper-globus"
    assert m.storage.prefix == "lab/data_foo/"
    assert m.storage.endpoint == "https://s3.wasabisys.com"
    assert m.storage.versioning is True
    assert m.storage.dvc.remote_name == "data_foo"


def test_init_code_type_uses_bare_name_for_dir_and_storage(tmp_path: Path) -> None:
    """Slice 39: `mintd init code foo` scaffolds `foo/` (not `code_foo/`) and,
    on the labonly DVC path, names the remote `foo` with an S3 prefix derived
    from `foo`. The `code` fact lives in `project.type`, not a name prefix."""
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="code",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="https://s3.wasabisys.com",
        ops=fake,
    )
    assert project_path == tmp_path / "foo"
    m = _read_metadata(project_path)
    assert m.project.type == "code"
    assert m.project.full_name == "foo"
    assert m.storage is not None
    assert m.storage.prefix == "lab/foo/"
    assert m.storage.dvc.remote_name == "foo"
    assert len(fake.remote_add_calls) == 1
    assert fake.remote_add_calls[0]["name"] == "foo"
    assert fake.remote_add_calls[0]["url"] == "s3://cooper-globus/lab/foo/"


def test_init_project_calls_dvc_remote_add(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
        ops=fake,
    )
    assert len(fake.remote_add_calls) == 1
    call = fake.remote_add_calls[0]
    assert call["name"] == "data_foo"
    assert call["url"] == "s3://cooper-globus/lab/data_foo/"
    assert call["default"] is True
    # No profile passed -> None recorded (matches default-credential-chain
    # case where ~/.aws/credentials lacks a [mintd] section).
    assert call["profile"] is None


def test_init_project_threads_aws_profile_into_remote_add(tmp_path: Path) -> None:
    """Slice 30: profile threads through so consumers running raw
    `dvc pull` (outside mintd) pick up the right credentials."""
    fake = _FakeInitOps()
    init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
        profile="mintd",
        ops=fake,
    )
    assert fake.remote_add_calls[0]["profile"] == "mintd"


def test_init_project_licensed_uses_slug_at_bucket_root(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data",
        name="optumtest",
        target_dir=tmp_path,
        classification="licensed",
        slug="optum",
        bucket="cooper-globus",
        endpoint="",
        ops=fake,
    )
    m = _read_metadata(project_path)
    assert m.storage is not None
    assert m.storage.prefix == "optum/data_optumtest/"
    assert fake.remote_add_calls[0]["url"] == "s3://cooper-globus/optum/data_optumtest/"


def test_init_project_rollback_on_remote_add_failure(tmp_path: Path) -> None:
    """If dvc_remote_add raises, .dvc/ is removed and the exception
    re-raises. metadata.json is intentionally left in place."""
    from mintd._init_ops import InitOpError
    fake = _FakeInitOps(fail_on={"dvc_remote_add"})
    with pytest.raises(InitOpError, match="dvc_remote_add"):
        init_project(
            project_type="data",
            name="foo",
            target_dir=tmp_path,
            classification="labonly",
            bucket="cooper-globus",
            endpoint="",
            ops=fake,
        )
    assert not (tmp_path / "data_foo" / ".dvc").exists()
    # metadata.json and the sentinel are left in place — together they are
    # what lets the identical command resume; see the rerun test below.
    assert (tmp_path / "data_foo" / "metadata.json").exists()
    assert (tmp_path / "data_foo" / ".mintd-init-incomplete").exists()


def test_init_resumes_storage_after_rollback(tmp_path: Path) -> None:
    """The rerun the rollback promises must actually work.

    At HEAD the rollback comment said "rerunning init re-applies the storage
    block", but the rerun hit the destination guard first, so the project was
    wedged until the user hand-deleted metadata.json. Nothing tested it: the
    rollback test asserted the leave-in-place state and never reran.
    """
    project_path = tmp_path / "data_foo"
    kwargs = dict(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
    )

    with pytest.raises(InitOpError, match="dvc_remote_add"):
        init_project(ops=_FakeInitOps(fail_on={"dvc_remote_add"}), **kwargs)  # type: ignore[arg-type]

    sentinel = project_path / ".mintd-init-incomplete"
    assert sentinel.exists()
    # Something the user edited between the failed run and the rerun.
    readme = project_path / "README.md"
    readme.write_text("EDITED AFTER THE FAILURE\n", encoding="utf-8")

    _path, written = init_project(ops=_FakeInitOps(), **kwargs)  # type: ignore[arg-type]

    assert written == []  # resume wires storage; it does not re-render
    assert readme.read_text(encoding="utf-8") == "EDITED AFTER THE FAILURE\n"
    assert _read_metadata(project_path).storage is not None
    assert not sentinel.exists()


def _complete_tree(tmp_path: Path, *, full_name: str = "data_foo") -> Path:
    """Render a full scaffold so the resume predicate is actually reachable.

    `resuming` is `not force and len(collisions) == len(targets) and
    _resuming(...)`, so a test that seeds one or two files never reaches
    `_resuming` at all -- it refuses on the collision branch and pins nothing.
    """
    from mintd._templates import render_scaffold

    project_path = tmp_path / full_name
    project_path.mkdir()
    render_scaffold(
        project_type="data", name="foo", language="python", target_dir=project_path
    )
    return project_path


def test_init_refuses_rerun_without_sentinel(tmp_path: Path) -> None:
    """No sentinel, no resume -- even when the metadata looks exactly right.

    This is the case the infer-from-metadata-shape predicate could not tell
    apart: a hand-authored or half-migrated metadata.json with a matching
    name and no storage block would have been silently storage-patched.

    The tree is complete and the name matches, so the sentinel's absence is
    the ONLY thing standing between this project and a silent storage patch.
    """
    project_path = _complete_tree(tmp_path)
    (project_path / "metadata.json").write_text(
        json.dumps({"project": {"full_name": "data_foo"}, "storage": None}),
        encoding="utf-8",
    )
    assert not (project_path / ".mintd-init-incomplete").exists()

    fake = _FakeInitOps()
    with pytest.raises(InitDestinationExists):
        init_project(
            project_type="data",
            name="foo",
            target_dir=tmp_path,
            classification="labonly",
            bucket="cooper-globus",
            endpoint="",
            ops=fake,
        )
    assert fake.git_calls == []


def test_init_refuses_rerun_for_another_project(tmp_path: Path) -> None:
    """A sentinel is not a blank cheque: the name still has to match."""
    project_path = _complete_tree(tmp_path)
    (project_path / ".mintd-init-incomplete").write_text("", encoding="utf-8")
    (project_path / "metadata.json").write_text(
        json.dumps({"project": {"full_name": "data_other"}}), encoding="utf-8"
    )

    with pytest.raises(InitDestinationExists):
        init_project(
            project_type="data", name="foo", target_dir=tmp_path, ops=_FakeInitOps()
        )


@pytest.mark.parametrize(
    "body", ["{ not json", "[]", '"a string"', '{"project": {}}'], ids=
    ["truncated", "toplevel-list", "toplevel-string", "missing-key"],
)
def test_init_refuses_corrupt_metadata_without_traceback(
    tmp_path: Path, body: str
) -> None:
    """A corrupt metadata.json must make init refuse, not raise a parse error.

    The sentinel path parses a file the user can edit, so every read/parse/
    shape failure has to land as InitDestinationExists.
    """
    project_path = _complete_tree(tmp_path)
    (project_path / ".mintd-init-incomplete").write_text("", encoding="utf-8")
    (project_path / "metadata.json").write_text(body, encoding="utf-8")

    with pytest.raises(InitDestinationExists):
        init_project(
            project_type="data", name="foo", target_dir=tmp_path, ops=_FakeInitOps()
        )


def test_init_rollback_keeps_a_dvc_dir_it_did_not_create(tmp_path: Path) -> None:
    """The rollback must not delete a `.dvc/` mintd never created.

    `dvc init` is skipped when `.dvc/` already exists, so the rollback can now
    reach one the user made -- holding `config.local` (credentials, gitignored,
    unrecoverable) and `cache/` (unpushed blobs). Deleting those would be the
    same class of harm as commit 9eedd9f.
    """
    project_path = tmp_path / "data_foo"
    dvc_dir = project_path / ".dvc"
    (dvc_dir / "cache").mkdir(parents=True)
    (dvc_dir / "config").write_text("[core]\n", encoding="utf-8")
    (dvc_dir / "config.local").write_text(
        "secret_access_key = S3CRET\n", encoding="utf-8"
    )
    (dvc_dir / "cache" / "blob").write_text("unpushed\n", encoding="utf-8")

    fake = _FakeInitOps(fail_on={"dvc_remote_add"})
    with pytest.raises(InitOpError, match="dvc_remote_add"):
        init_project(
            project_type="data",
            name="foo",
            target_dir=tmp_path,
            force=True,
            classification="labonly",
            bucket="cooper-globus",
            endpoint="",
            ops=fake,
        )

    assert (dvc_dir / "config").exists()
    assert (dvc_dir / "config.local").read_text(encoding="utf-8") == (
        "secret_access_key = S3CRET\n"
    )
    assert (dvc_dir / "cache" / "blob").read_text(encoding="utf-8") == "unpushed\n"
    assert fake.dvc_calls == []  # dvc_init skipped: .dvc/ already there


def test_init_refuses_resume_over_a_partial_scaffold(tmp_path: Path) -> None:
    """A crash mid-render leaves the sentinel and a matching metadata.json --
    metadata.json is written 2nd of 14 -- but resuming there would skip the
    render and report success over a tree with no .gitignore and no dvc.yaml."""
    project_path = tmp_path / "data_foo"
    project_path.mkdir()
    (project_path / ".mintd-init-incomplete").write_text("", encoding="utf-8")
    (project_path / "README.md").write_text("partial\n", encoding="utf-8")
    (project_path / "metadata.json").write_text(
        json.dumps({"project": {"full_name": "data_foo"}}), encoding="utf-8"
    )

    with pytest.raises(InitDestinationExists):
        init_project(
            project_type="data", name="foo", target_dir=tmp_path, ops=_FakeInitOps()
        )


def test_init_resume_maps_invalid_metadata_to_init_op_error(tmp_path: Path) -> None:
    """Resume re-reads a file the user may have edited between the runs, so a
    pydantic ValidationError must not reach the CLI as a raw traceback."""
    kwargs = dict(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
    )
    with pytest.raises(InitOpError, match="dvc_remote_add"):
        init_project(ops=_FakeInitOps(fail_on={"dvc_remote_add"}), **kwargs)  # type: ignore[arg-type]

    # The user "fixes" metadata.json and drops a required block.
    metadata_path = tmp_path / "data_foo" / "metadata.json"
    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    raw.pop("governance", None)
    metadata_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(InitOpError, match="not valid mintd metadata"):
        init_project(ops=_FakeInitOps(), **kwargs)  # type: ignore[arg-type]


def test_init_refuses_to_repoint_a_remote_it_did_not_create(tmp_path: Path) -> None:
    """A same-named remote pointing elsewhere is somebody else's.

    Whether the remote is mintd's to rewrite is a question about the remote,
    not about how init got here: neither "mintd crashed here once" nor
    --force says anything about who put it there. Repointing it would send
    the user's next `dvc push` to a bucket they did not choose.
    """
    project_path = tmp_path / "data_foo"
    (project_path / ".dvc").mkdir(parents=True)

    fake = _FakeInitOps()
    fake.existing_remotes["data_foo"] = "s3://someone-elses-bucket/private"

    # A file the user wrote, which --force would otherwise replace.
    readme = project_path / "README.md"
    readme.write_text("MY NOTES\n", encoding="utf-8")

    with pytest.raises(InitOpError, match="already points at"):
        init_project(
            project_type="data",
            name="foo",
            target_dir=tmp_path,
            force=True,
            classification="labonly",
            bucket="cooper-globus",
            endpoint="",
            ops=fake,
        )
    assert fake.existing_remotes["data_foo"] == "s3://someone-elses-bucket/private"
    # The refusal is a preflight check, so it must land before the render --
    # otherwise the user loses their files and still gets no project.
    assert readme.read_text(encoding="utf-8") == "MY NOTES\n"
    assert fake.git_calls == []


def test_init_force_completes_over_its_own_finished_project(tmp_path: Path) -> None:
    """--force over a project mintd already wired must finish, not half-run.

    The remote is already exactly what mintd would write, so re-adding it is
    ours to do. Getting this wrong re-rendered every file and THEN died on
    dvc's "remote already exists", leaving storage unwired.
    """
    kwargs = dict(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
    )
    fake = _FakeInitOps()
    project_path, _ = init_project(ops=fake, **kwargs)  # type: ignore[arg-type]
    assert _read_metadata(project_path).storage is not None

    # .dvc/ survives a real init, so the rerun skips dvc_init as it would live.
    (project_path / ".dvc").mkdir(exist_ok=True)

    init_project(ops=fake, force=True, **kwargs)  # type: ignore[arg-type]
    assert _read_metadata(project_path).storage is not None
    assert not (project_path / ".mintd-init-incomplete").exists()
    # The rerun must not try to ADD the remote again -- real dvc fails on a
    # duplicate without -f, and -f would replace the section.
    assert fake.remote_add_calls[-1]["exists"] is True


def test_init_rerun_keeps_endpoint_and_profile_on_the_existing_remote(
    tmp_path: Path,
) -> None:
    """A rerun must not strip endpointurl/profile from the tracked config.

    `dvc remote add -f` replaces the section rather than merging, and the
    machine doing the rerun may not be able to supply those values at all --
    SSO or env-var auth gives no [mintd] profile. .dvc/config is tracked and
    mintd stages it, so the loss would be committed for everyone.
    """
    kwargs = dict(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
    )
    fake = _FakeInitOps()
    project_path, _ = init_project(  # type: ignore[arg-type]
        ops=fake, endpoint="https://minio.lab", profile="mintd", **kwargs
    )
    (project_path / ".dvc").mkdir(exist_ok=True)
    assert fake.remote_configs["data_foo"]["endpointurl"] == "https://minio.lab"

    # Second machine: same bucket, but no endpoint and no [mintd] profile.
    init_project(  # type: ignore[arg-type]
        ops=fake, force=True, endpoint="", profile=None, **kwargs
    )
    cfg = fake.remote_configs["data_foo"]
    assert cfg["endpointurl"] == "https://minio.lab"
    assert cfg["profile"] == "mintd"


def test_init_resume_finishes_a_half_configured_remote(tmp_path: Path) -> None:
    """A run interrupted between `remote add` and `remote modify` must be
    completed by the resume, not declared finished.

    Only the first of dvc_remote_add's steps writes the URL, so matching on
    the URL alone would treat a remote with no endpointurl / profile /
    version_aware as fully configured -- and the state most likely to need
    resuming is exactly that one.
    """
    kwargs = dict(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="https://minio.lab",
        profile="mintd",
    )
    project_path = tmp_path / "data_foo"
    (project_path / ".dvc").mkdir(parents=True)

    fake = _FakeInitOps()
    # The interrupted state: URL written, nothing after it.
    fake.existing_remotes["data_foo"] = "s3://cooper-globus/lab/data_foo/"

    init_project(ops=fake, force=True, **kwargs)  # type: ignore[arg-type]

    cfg = fake.remote_configs["data_foo"]
    assert cfg["endpointurl"] == "https://minio.lab"
    assert cfg["profile"] == "mintd"
    assert cfg["version_aware"] == "true"
    assert fake.default_remote == "data_foo"


def test_init_sentinel_does_not_write_through_a_symlink(tmp_path: Path) -> None:
    """The sentinel is not a scaffold target, so _escapes never sees it --
    but write_text would still follow a symlink there and truncate the
    target. It gets the same unlink-first treatment as rendered files."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")

    project_path = tmp_path / "my_proj"
    project_path.mkdir()
    (project_path / ".mintd-init-incomplete").symlink_to(secret)

    init_project(
        project_type="code",
        name="my_proj",
        target_dir=tmp_path,
        use_current_repo=True,
        ops=_FakeInitOps(),
    )
    assert secret.read_text(encoding="utf-8") == "TOP SECRET\n"


def test_init_removes_sentinel_on_success(tmp_path: Path) -> None:
    """A finished project carries no crash marker."""
    project_path, _ = init_project(
        project_type="data", name="foo", target_dir=tmp_path, ops=_FakeInitOps()
    )
    assert not (project_path / ".mintd-init-incomplete").exists()


def test_init_project_requires_bucket_when_classification_set(tmp_path: Path) -> None:
    from mintd._init_ops import InitOpError
    fake = _FakeInitOps()
    with pytest.raises(InitOpError, match="bucket not configured"):
        init_project(
            project_type="data",
            name="foo",
            target_dir=tmp_path,
            classification="labonly",
            bucket=None,
            ops=fake,
        )


def test_init_project_legacy_path_unchanged_when_classification_none(tmp_path: Path) -> None:
    """Backward compat: omitting classification skips storage wiring
    entirely (existing tests rely on this)."""
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
    )
    m = _read_metadata(project_path)
    assert m.storage is None
    assert fake.remote_add_calls == []


def test_init_project_patches_storage_even_when_template_emits_partial_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive raw-dict pop (round-2 P0 fix): if a template ever emits
    a partial storage placeholder, init_project's patch survives by
    popping ``storage`` from the raw dict before Pydantic validation.

    Wraps the real render_scaffold and inject a partial storage block
    after — simulates a future template regression without hand-crafting
    a full Metadata fixture.
    """
    import json
    from mintd import init as init_mod
    fake = _FakeInitOps()
    real_render = init_mod.render_scaffold

    def _wrap_with_poison(*, project_type, name, language, target_dir):
        written = real_render(
            project_type=project_type, name=name,
            language=language, target_dir=target_dir,
        )
        meta_path = target_dir / "metadata.json"
        raw = json.loads(meta_path.read_text())
        raw["storage"] = {"bucket": ""}  # partial placeholder; missing required fields
        meta_path.write_text(json.dumps(raw))
        return written

    monkeypatch.setattr("mintd.init.render_scaffold", _wrap_with_poison)

    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
        ops=fake,
    )
    m = _read_metadata(project_path)
    assert m.storage is not None
    assert m.storage.bucket == "cooper-globus"
    assert m.storage.prefix == "lab/data_foo/"


# ---------------------------------------------------------------------------
# F4 — restage .dvc/config after init (git_add) + rollback unstage
# ---------------------------------------------------------------------------


class _WarnRecorder(Reporter):
    """Reporter subclass that records ``warn`` calls instead of printing.

    Local to this module so the F4 tests can assert *exactly one* restage
    warning fired on failure and *zero* on success, without depending on
    stderr capture or touching the shared RecordingReporter fake.
    """

    def __init__(self) -> None:
        super().__init__()
        self.warnings: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def test_init_restages_dvc_config_after_dvc_init_no_classification(
    tmp_path: Path,
) -> None:
    """The `dvc config cache.type` write inside `dvc_init` dirties the
    staged `.dvc/config` even when classification is None, so the restage
    must fire on the plain data path — once, and after `dvc_init`."""
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="foo", target_dir=tmp_path, ops=fake
    )
    assert fake.git_add_calls == [(project_path, [".dvc/config"])]
    # restage happens after dvc was initialized
    assert fake.call_log.index("git_add") > fake.call_log.index("dvc_init")


def test_init_restages_dvc_config_after_remote_add_when_classified(
    tmp_path: Path,
) -> None:
    """With classification set, `dvc remote add` also rewrites
    `.dvc/config`; the single restage must land after remote-add."""
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
        ops=fake,
    )
    assert fake.git_add_calls == [(project_path, [".dvc/config"])]
    assert fake.call_log.index("git_add") > fake.call_log.index("dvc_remote_add")


def test_init_enclave_does_not_restage_dvc_config(tmp_path: Path) -> None:
    """Enclave is not a DVC type (`_DVC_INIT_TYPES`), so there is no
    `.dvc/config` to restage."""
    fake = _FakeInitOps()
    init_project(
        project_type="enclave", name="foo", target_dir=tmp_path, ops=fake
    )
    assert fake.git_add_calls == []


def test_init_rollback_unstages_dvc_and_skips_restage(tmp_path: Path) -> None:
    """On a remote-add failure the rollback rmtree's `.dvc/` and unstages
    the `.dvc/*` index entries `dvc init` left behind; the restage never
    runs (its config target no longer exists)."""
    fake = _FakeInitOps(fail_on={"dvc_remote_add"})
    with pytest.raises(InitOpError, match="dvc_remote_add"):
        init_project(
            project_type="data",
            name="foo",
            target_dir=tmp_path,
            classification="labonly",
            bucket="cooper-globus",
            endpoint="",
            ops=fake,
        )
    assert fake.git_unstage_calls == [(tmp_path / "data_foo", [".dvc"])]
    assert fake.git_add_calls == []


def test_init_failed_restage_warns_once_and_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed restage must not fail an otherwise-healthy init: init
    returns success and the reporter records exactly one actionable warn.

    Config is pinned because "exactly one" is now shared with unit G's
    empty-github_url warning: without an org configured that warning fires
    too, and this count would depend on the machine running it."""
    _isolated_config(monkeypatch, tmp_path, registry_org="acme")
    fake = _FakeInitOps(fail_on={"git_add"})
    reporter = _WarnRecorder()
    project_path, written = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )
    assert (project_path / "metadata.json").exists()
    assert len(reporter.warnings) == 1
    assert "git add .dvc/config" in reporter.warnings[0]


def test_init_successful_restage_emits_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restage warning fires only on failure — a clean restage is
    silent (zero warns). Config pinned for the same reason as above."""
    _isolated_config(monkeypatch, tmp_path, registry_org="acme")
    fake = _FakeInitOps()
    reporter = _WarnRecorder()
    init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )
    assert reporter.warnings == []


def test_init_failed_restage_without_reporter_still_succeeds(
    tmp_path: Path,
) -> None:
    """Reporterless callers (library/tests) still get a healthy project on
    a restage failure — the warn is simply skipped, never a raise."""
    fake = _FakeInitOps(fail_on={"git_add"})
    project_path, _ = init_project(
        project_type="data", name="foo", target_dir=tmp_path, ops=fake
    )
    assert (project_path / "metadata.json").exists()


# ---------------------------------------------------------------------------
# Unit G, S3a + S3b — init warns about the empty repository.github_url it just
# wrote, and names the repo's git origin as a candidate. Unit G's render and
# `check` halves live in tests/test_repository_identity.py; these two slices
# are init behavior, so they sit here beside _WarnRecorder, _FakeInitOps, and
# the argv-pinned seam tests they reuse rather than duplicate.
# ---------------------------------------------------------------------------


def _isolated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, registry_org: str | None
) -> None:
    """Point ``Config.load()`` at a config dir this test owns.

    Load-bearing, not hygiene. `_render` derives repository.github_url from
    `registry_org` (`_render.py:178`) through the real `Config.load()`, so
    without this the assertions below would pass or fail depending on whether
    whoever runs them happens to have an org configured -- and that genuinely
    differs between a lab laptop and CI.
    """
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    if registry_org is not None:
        (cfg_dir / "config.yaml").write_text(
            f"registry_org: {registry_org}\n", encoding="utf-8"
        )
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(cfg_dir))


def _url_of(project_path: Path) -> str:
    raw = json.loads((project_path / "metadata.json").read_text(encoding="utf-8"))
    return raw["repository"]["github_url"]


def test_init_warns_when_it_wrote_an_empty_github_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `registry_org` means nothing to derive, so init writes an empty
    github_url -- and `mintd check` rejects that at severity=error, which
    publish then refuses. Init must say so where the value is written, not
    leave the user to discover it on their next `check`."""
    _isolated_config(monkeypatch, tmp_path, registry_org=None)
    fake = _FakeInitOps()
    reporter = _WarnRecorder()

    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )

    assert _url_of(project_path) == ""
    assert len(reporter.warnings) == 1
    warning = reporter.warnings[0]
    # The three things the user needs: which field, what will reject it, the fix.
    assert "repository.github_url" in warning
    assert "mintd check" in warning
    assert "mintd config setup" in warning


def test_init_does_not_warn_when_the_github_url_was_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over-fire guard: the healthy path is silent. Deleting the emptiness
    check in init (warn unconditionally) reddens exactly here."""
    _isolated_config(monkeypatch, tmp_path, registry_org="acme")
    fake = _FakeInitOps()
    reporter = _WarnRecorder()

    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )

    assert _url_of(project_path) == "https://github.com/acme/data_foo"
    assert reporter.warnings == []
    # And no pointless subprocess on the healthy path.
    assert "git_origin_url" not in fake.call_log


def test_init_empty_url_warning_suggests_the_git_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3b: the warning names a candidate rather than only saying "empty".

    The fake returns the raw scp-like form git actually prints, so init's own
    normalizer is what runs -- if the fake normalized instead, this would pass
    no matter what production does with `git@...` (the lesson of 7c5fe05).
    """
    _isolated_config(monkeypatch, tmp_path, registry_org=None)
    fake = _FakeInitOps()
    fake.origin_url = "git@github.com:acme/data_foo.git"
    reporter = _WarnRecorder()

    init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )

    assert len(reporter.warnings) == 1
    assert "https://github.com/acme/data_foo" in reporter.warnings[0]
    # A suggestion, never a fallback: nothing wrote it into the file.
    assert _url_of(tmp_path / "data_foo") == ""


def test_init_empty_url_warning_omits_the_candidate_without_an_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary path: `git init` just made the repo, so there is no origin
    to name. The base warning still fires; it simply has no candidate."""
    _isolated_config(monkeypatch, tmp_path, registry_org=None)
    fake = _FakeInitOps()
    fake.origin_url = None
    reporter = _WarnRecorder()

    init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )

    assert len(reporter.warnings) == 1
    assert "repository.github_url" in reporter.warnings[0]
    assert "origin" not in reporter.warnings[0]


def test_init_reports_a_non_github_origin_as_it_found_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No host filter, deliberately. The message reports what origin IS and a
    human confirms it before anything is written -- and filtering here would
    contradict `check`, which validates presence only and never asserts the
    derived shape (mirrors, forks, renames)."""
    _isolated_config(monkeypatch, tmp_path, registry_org=None)
    fake = _FakeInitOps()
    fake.origin_url = "git@gitlab.com:acme/foo.git"
    reporter = _WarnRecorder()

    init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )

    assert "https://gitlab.com/acme/foo" in reporter.warnings[0]


def test_init_survives_a_failing_origin_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken origin lookup costs the suggestion, not the init.

    This block runs after an otherwise-healthy init, where the surrounding code
    goes out of its way to never raise (restage, sentinel) -- a traceback here
    would fail a project that is already on disk and fine.
    """
    _isolated_config(monkeypatch, tmp_path, registry_org=None)
    fake = _FakeInitOps(fail_on={"git_origin_url"})
    reporter = _WarnRecorder()

    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        ops=fake,
        reporter=reporter,
    )

    assert (project_path / "metadata.json").exists()
    assert len(reporter.warnings) == 1
    assert "repository.github_url" in reporter.warnings[0]
    assert "origin" not in reporter.warnings[0]


def test_https_remote_normalizes_the_forms_git_prints() -> None:
    """Normalization lives in init, not in the seam, so the fake cannot pin
    its own version of it. Table-driven because each row is a form git really
    emits."""
    from mintd.init import _https_remote

    assert _https_remote("git@github.com:acme/foo.git") == "https://github.com/acme/foo"
    assert _https_remote("https://github.com/acme/foo.git") == "https://github.com/acme/foo"
    assert _https_remote("git@gitlab.com:acme/foo.git") == "https://gitlab.com/acme/foo"
    assert _https_remote("  git@github.com:acme/foo  ") == "https://github.com/acme/foo"
    # Not scp-like: kept as found rather than guessed at.
    assert _https_remote("/srv/git/foo.git") == "/srv/git/foo"
    assert _https_remote("ssh://git@github.com/acme/foo") == "ssh://git@github.com/acme/foo"


def test_git_origin_url_argv_and_absent_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the argv, and that a non-zero exit reads as "no origin".

    Same reason as test_dvc_remote_url_reads_project_scope_only: the fake
    returns whatever a test set on it, so only this pins what the real
    function asks git.
    """
    import subprocess

    from mintd._init_ops import SubprocessInitOps

    calls: list[list[str]] = []

    class _R:
        def __init__(self, rc: int, out: str) -> None:
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def make(rc: int, out: str):
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return _R(rc, out)
        return fake_run

    monkeypatch.setattr(subprocess, "run", make(0, "git@github.com:acme/foo.git\n"))
    assert SubprocessInitOps().git_origin_url(tmp_path) == "git@github.com:acme/foo.git"
    assert calls[0] == ["git", "remote", "get-url", "origin"]

    # git exits 2 ("error: No such remote 'origin'") on a repo with no origin.
    monkeypatch.setattr(subprocess, "run", make(2, ""))
    assert SubprocessInitOps().git_origin_url(tmp_path) is None

    # Non-zero WITH output on stdout is what the returncode check is actually
    # for -- absent an explicit check, `stdout.strip() or None` would hand this
    # back as if it were a remote URL. Empty-stdout failures cannot tell the
    # two apart, so this case is the one that pins the guard.
    monkeypatch.setattr(subprocess, "run", make(2, "warning: whatever\n"))
    assert SubprocessInitOps().git_origin_url(tmp_path) is None


def test_git_origin_url_reads_a_real_repo_live(tmp_path: Path) -> None:
    """Live seam: argv-pinning proves we issue what we meant, not that git
    understands it. A real `git init` + `git remote add` closes that."""
    import shutil
    import subprocess

    from mintd._init_ops import SubprocessInitOps

    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    ops = SubprocessInitOps()
    assert ops.git_origin_url(tmp_path) is None  # no origin yet

    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/foo.git"],
        cwd=tmp_path,
        check=True,
    )
    assert ops.git_origin_url(tmp_path) == "git@github.com:acme/foo.git"


def test_subprocess_git_add_restages_dvc_config_live(tmp_path: Path) -> None:
    """Live seam: a real `git init` + `dvc init` leaves `.dvc/config`
    staged-then-modified (`AM`, because the cache.type write rewrites it);
    `SubprocessInitOps.git_add` must restage it to a clean `A `."""
    import shutil
    import subprocess

    from mintd._init_ops import SubprocessInitOps

    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    try:
        import dvc  # noqa: F401
    except ImportError:
        pytest.skip("dvc not importable")

    ops = SubprocessInitOps()
    ops.git_init(tmp_path)
    ops.dvc_init(tmp_path)

    def _status() -> str:
        return subprocess.run(
            ["git", "status", "--porcelain", "--", ".dvc/config"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        ).stdout

    # Before restage: staged by `dvc init`, then dirtied by cache.type.
    assert _status().startswith("AM")
    ops.git_add(tmp_path, [".dvc/config"])
    # After restage: index clean ("A " — added, no worktree delta).
    after = _status()
    assert after.startswith("A ")
    assert not after.startswith("AM")


def test_subprocess_git_init_lands_on_main_branch_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git init` must produce a repo on `main`, not `master`.

    `_render.py:235` hardcodes `"default_branch": "main"` into the
    metadata.json written by the same `mintd init` call, so a bare
    `git init` on a machine without `init.defaultBranch` contradicts the
    metadata mintd just wrote.

    Both GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM are scrubbed, because a
    machine with `init.defaultBranch=main` set globally MASKS this entirely
    -- the test would pass without the fix and prove nothing. They point at
    an empty file rather than /dev/null so this also runs on windows-test.
    """
    import shutil
    import subprocess

    from mintd._init_ops import SubprocessInitOps

    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))

    repo = tmp_path / "repo"
    repo.mkdir()
    SubprocessInitOps().git_init(repo)
    head = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert head == "main", f"new repo landed on {head!r}, not 'main'"


def test_subprocess_git_init_leaves_an_existing_branch_alone_live(
    tmp_path: Path,
) -> None:
    """Re-running init over an existing repo must not rename its branch.

    `git init -b main` on an already-initialized repo prints
    `warning: re-init: ignored --initial-branch=main` and exits 0. So
    `--use-current-repo` into a repo already on `master` keeps `master`,
    even though metadata records `main`. That is correct -- mintd must not
    rename someone's branch -- and is pinned here so it is not "fixed" later
    as a bug.
    """
    import shutil
    import subprocess

    from mintd._init_ops import SubprocessInitOps

    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "master", "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    SubprocessInitOps().git_init(repo)
    head = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "master"


# ---------------------------------------------------------------------------
# Slice 30 — _prompt_classification (interactive prompt)
# ---------------------------------------------------------------------------

def test_prompt_classification_non_tty_raises() -> None:
    from mintd._console import Reporter
    from mintd._init_ops import InitNonInteractive
    from mintd.init import _prompt_classification
    with pytest.raises(InitNonInteractive):
        _prompt_classification(
            reporter=Reporter(),
            prompt_fn=lambda _: "1",
            isatty_fn=lambda: False,
        )


def test_prompt_classification_labonly_no_slug() -> None:
    from mintd._console import Reporter
    from mintd.init import _prompt_classification
    tier, slug = _prompt_classification(
        reporter=Reporter(),
        prompt_fn=lambda _: "1",
        isatty_fn=lambda: True,
    )
    assert tier == "labonly"
    assert slug is None


def test_prompt_classification_licensed_prompts_for_slug() -> None:
    from mintd._console import Reporter
    from mintd.init import _prompt_classification
    inputs = iter(["3", "optum"])
    tier, slug = _prompt_classification(
        reporter=Reporter(),
        prompt_fn=lambda _: next(inputs),
        isatty_fn=lambda: True,
    )
    assert tier == "licensed"
    assert slug == "optum"


def test_init_then_inspect_returns_initialized(tmp_path: Path) -> None:
    """Integration: a freshly init'd project classifies as INITIALIZED.

    NOTE: _FakeInitOps doesn't actually write .dvc/config (the real
    SubprocessInitOps does via subprocess), so we simulate it post-hoc
    so inspect_storage has both sides to compare.
    """
    from mintd._storage_state import StorageState, inspect_storage
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data",
        name="foo",
        target_dir=tmp_path,
        classification="labonly",
        bucket="cooper-globus",
        endpoint="",
        ops=fake,
    )
    # Simulate what SubprocessInitOps.dvc_remote_add would write to disk
    dvc_cfg = project_path / ".dvc" / "config"
    dvc_cfg.parent.mkdir(parents=True, exist_ok=True)
    dvc_cfg.write_text(
        "[core]\n    remote = data_foo\n"
        '[remote "data_foo"]\n    url = s3://cooper-globus/lab/data_foo/\n'
    )
    assert inspect_storage(project_path).state == StorageState.INITIALIZED


# ---------------------------------------------------------------------------
# Slice 30 polish — SubprocessInitOps.dvc_init configures cache.type
# ---------------------------------------------------------------------------

def test_subprocess_dvc_init_sets_cache_type_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production InitOps must follow `dvc init` with
    `dvc config cache.type reflink,hardlink,symlink,copy` so freshly-
    init'd projects don't fall back to slow copy mode on Linux ext4.
    Per-project scope (no --local / --global) so consumers cloning the
    repo inherit the setting."""
    import subprocess
    from mintd._init_ops import SubprocessInitOps

    calls: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessInitOps().dvc_init(tmp_path)

    assert calls[0] == [*dvc_cmd(), "init"]
    assert calls[1] == [
        *dvc_cmd(), "config", "cache.type",
        "reflink,hardlink,symlink,copy",
    ]


# ---------------------------------------------------------------------------
# Slice 33 — version_aware default on every dvc_remote_add
# ---------------------------------------------------------------------------


def test_dvc_remote_add_issues_version_aware_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `dvc remote add` mintd performs must be immediately followed
    by `dvc remote modify <name> version_aware true`, unconditional —
    regardless of whether endpoint/profile are set. Path-based S3 keys
    are mintd's mental model (matches what fast-sync, data_ops, and
    `data ls` already assume), and `metadata.storage.versioning = True`
    is already declared producer-side."""
    import subprocess
    from mintd._init_ops import SubprocessInitOps

    calls: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessInitOps().dvc_remote_add(
        tmp_path,
        name="data_x",
        url="s3://b/k/",
        default=True,
        endpoint=None,
        profile=None,
    )

    assert calls[0] == [*dvc_cmd(), "remote", "add", "-d", "data_x", "s3://b/k/"]
    assert [*dvc_cmd(), "remote", "modify", "data_x", "version_aware", "true"] in calls


def test_dvc_remote_add_exists_skips_only_the_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`exists=True` must skip the add and NOTHING else.

    Only the add writes the URL, so a run interrupted after it leaves a
    remote with no endpointurl / profile / version_aware -- and that is
    precisely the state a resume exists to finish. `dvc remote default`
    stands in for the `-d` the skipped add would have set.

    Pinned against the real argv, not the fake: the fake sets its own
    default_remote off the kwarg, so it would satisfy a policy assertion no
    matter what this function actually issues.
    """
    import subprocess
    from mintd._init_ops import SubprocessInitOps

    calls: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessInitOps().dvc_remote_add(
        tmp_path,
        name="data_x",
        url="s3://b/k/",
        default=True,
        endpoint="https://minio.lab",
        profile="mintd",
        exists=True,
    )

    # No `remote add` at all -- it would need -f, which replaces the section.
    assert not any(c[-3:-2] == ["add"] or "add" in c[:4] for c in calls), calls
    assert calls[0] == [*dvc_cmd(), "remote", "default", "data_x"]
    # ...but every option step still runs.
    for tail in (
        ["remote", "modify", "data_x", "endpointurl", "https://minio.lab"],
        ["remote", "modify", "data_x", "profile", "mintd"],
        ["remote", "modify", "data_x", "version_aware", "true"],
    ):
        assert [*dvc_cmd(), *tail] in calls, calls


def test_dvc_remote_url_reads_project_scope_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe must use `dvc config --project`, and map a non-zero exit
    to "absent".

    `--project` is load-bearing: plain `dvc config` merges the user's GLOBAL
    config, so on a lab machine that already has a global remote of this
    name a genuinely fresh init would look like a collision and be refused.
    """
    import subprocess
    from mintd._init_ops import SubprocessInitOps

    calls: list[list[str]] = []

    class _R:
        def __init__(self, rc: int, out: str) -> None:
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def make(rc: int, out: str):
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return _R(rc, out)
        return fake_run

    monkeypatch.setattr(subprocess, "run", make(0, "s3://b/k/\n"))
    assert SubprocessInitOps().dvc_remote_url(tmp_path, "data_x") == "s3://b/k/"
    assert calls[0] == [*dvc_cmd(), "config", "--project", "remote.data_x.url"]

    # dvc exits 251 for a remote that is not in the project config.
    monkeypatch.setattr(subprocess, "run", make(251, ""))
    assert SubprocessInitOps().dvc_remote_url(tmp_path, "data_x") is None


def test_dvc_remote_add_version_aware_fires_after_endpoint_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With endpoint + profile set, the call order is: add, modify
    endpointurl, modify profile, modify version_aware. Version_aware is
    last and unconditional."""
    import subprocess
    from mintd._init_ops import SubprocessInitOps

    calls: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessInitOps().dvc_remote_add(
        tmp_path,
        name="data_y",
        url="s3://b/k/",
        default=True,
        endpoint="https://s3.example",
        profile="mintd",
    )

    assert calls == [
        [*dvc_cmd(), "remote", "add", "-d", "data_y", "s3://b/k/"],
        [*dvc_cmd(), "remote", "modify", "data_y", "endpointurl", "https://s3.example"],
        [*dvc_cmd(), "remote", "modify", "data_y", "profile", "mintd"],
        [*dvc_cmd(), "remote", "modify", "data_y", "version_aware", "true"],
    ]


def test_dvc_remote_add_version_aware_failure_raises_init_op_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `dvc remote modify <name> version_aware true` exits nonzero,
    `dvc_remote_add` raises `InitOpError` with the stderr included so the
    caller's rollback path (init.py:172-177 rmtree of .dvc/) fires."""
    import subprocess
    from mintd._init_ops import InitOpError, SubprocessInitOps
    from mintd._dvc_invoke import dvc_cmd

    def fake_run(argv, **kwargs):
        class _R:
            stdout = ""
            stderr = ""
            returncode = 0
        r = _R()
        if list(argv[:len(dvc_cmd()) + 2]) == [*dvc_cmd(), "remote", "modify"] and "version_aware" in argv:
            r.returncode = 1
            r.stderr = "boom"
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(InitOpError, match="version_aware"):
        SubprocessInitOps().dvc_remote_add(
            tmp_path,
            name="data_z",
            url="s3://b/k/",
            default=True,
            endpoint=None,
            profile=None,
        )

