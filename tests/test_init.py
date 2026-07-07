from __future__ import annotations

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
    # metadata.json left in place — rerunning init re-applies the patch
    assert (tmp_path / "data_foo" / "metadata.json").exists()


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
    tmp_path: Path,
) -> None:
    """A failed restage must not fail an otherwise-healthy init: init
    returns success and the reporter records exactly one actionable warn."""
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


def test_init_successful_restage_emits_no_warning(tmp_path: Path) -> None:
    """The restage warning fires only on failure — a clean restage is
    silent (zero warns)."""
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

