"""Tests for the Jinja-rendered scaffold engine (slice 19)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mintd._templates import (
    InitNameInvalid,
    render_scaffold,
    render_template,
    validate_project_name,
)
from mintd._templates.scaffolds import dispatch
from mintd.model import Metadata


# --- engine + name validation ---------------------------------------------

def test_render_template_substitutes_project_name() -> None:
    context = _MIN_CONTEXT | {"project_name": "alpha"}
    out = render_template("README_data.md.j2", context)
    assert "alpha" in out
    assert "{{" not in out
    assert "{%" not in out


def test_validate_project_name_rejects_leading_dash() -> None:
    with pytest.raises(InitNameInvalid):
        validate_project_name("-bad")


def test_validate_project_name_accepts_underscore_and_digits() -> None:
    validate_project_name("my_project_2")  # no exception


# --- per-type scaffolds ---------------------------------------------------

def test_data_python_renders_all_files(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "metadata.json" in rel
    assert "README.md" in rel
    assert "requirements.txt" in rel
    assert "code/fetch.py" in rel
    assert "code/ingest.py" in rel
    # Slice: the clean.* stub was deleted (demoted into ingest's parse_and_clean
    # helper); the scaffold must no longer emit it.
    assert "code/clean.py" not in rel
    assert "code/validate.py" in rel
    # Slice 41: scaffold no longer ships generate_schema.py; the CLI
    # (`mintd data schema generate`) replaces the vendored script.
    assert "schemas/generate_schema.py" not in rel
    assert "scripts/check-dvc-sync.sh" in rel


def test_project_python_renders_all_files(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="project", name="foo", language="python", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "metadata.json" in rel
    assert "citations.md" in rel
    assert "code/config.py" in rel
    assert "code/02_analysis/__init__.py" in rel


def test_code_renders_metadata_only(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="code", name="foo", language="python", target_dir=tmp_path
    )
    assert [p.relative_to(tmp_path).as_posix() for p in written] == ["metadata.json"]
    # Pin the Pydantic bypass: code-type metadata must validate AND carry
    # `project.type == "code"`. Regression coverage for a hardcoded "data".
    metadata = Metadata.model_validate_json((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata.project.type == "code"
    assert metadata.project.name == "foo"
    assert metadata.project.full_name == "foo"


@pytest.mark.parametrize(
    ("project_type", "expected_full_name"),
    [
        ("data", "data_foo"),
        ("project", "prj_foo"),
        ("enclave", "enclave_foo"),
        ("code", "foo"),
    ],
)
def test_full_name_prefix_convention(
    tmp_path: Path, project_type: str, expected_full_name: str
) -> None:
    """`code` carries the bare name; every other type keeps `<type>_<name>`
    (slice 39). `name` and `type` are unchanged across types."""
    render_scaffold(
        project_type=project_type, name="foo", language="python", target_dir=tmp_path
    )
    metadata = Metadata.model_validate_json(
        (tmp_path / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata.project.type == project_type
    assert metadata.project.name == "foo"
    assert metadata.project.full_name == expected_full_name


def test_enclave_renders_transfer_scripts(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "enclave_manifest.yaml" in rel
    assert "enclave_cli.py" in rel
    assert "scripts/pull_data.sh" in rel
    assert "src/registry.py" in rel
    # enclave does NOT get the language-specific data files.
    assert "code/ingest.py" not in rel


# --- language matrix ------------------------------------------------------

def test_data_r_renders_r_sources(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="r", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "code/fetch.R" in rel
    assert "code/ingest.R" in rel
    assert "code/clean.R" not in rel
    assert "DESCRIPTION" in rel
    assert "renv.lock" in rel
    assert "code/ingest.py" not in rel


def test_data_stata_renders_do_sources(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="stata", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "code/fetch.do" in rel
    assert "code/ingest.do" in rel
    assert "code/clean.do" not in rel
    assert "stata-packages.txt" in rel
    assert "code/ingest.py" not in rel


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_data_dvc_yaml_pipeline_shape(tmp_path: Path, language: str) -> None:
    """Rendered dvc.yaml must match the immutable-raw governance model:
    ingest reads raw + writes intermediate; clean is not a stage; no active
    stage writes data/raw/; fetch ships only as a commented-out block."""
    import yaml

    render_scaffold(
        project_type="data", name="foo", language=language, target_dir=tmp_path
    )
    raw_text = (tmp_path / "dvc.yaml").read_text(encoding="utf-8")
    stages = yaml.safe_load(raw_text)["stages"]

    ingest = stages["ingest"]
    assert "../data/raw/" in ingest["deps"], "ingest must read data/raw/"
    assert ingest["outs"] == ["../data/intermediate/"], (
        "ingest must write data/intermediate/"
    )

    assert "clean" not in stages, "clean must not be a DVC stage (demoted to helper)"

    validate = stages["validate"]
    assert "../data/intermediate/" in validate["deps"]
    assert "../data/raw/" not in validate.get("deps", [])

    # Immutability invariant: no active stage declares data/raw/ as an out.
    for stage_name, stage in stages.items():
        assert "../data/raw/" not in stage.get("outs", []), (
            f"stage {stage_name!r} writes data/raw/ — immutability violation"
        )

    # fetch ships as a commented-out block, not an active stage.
    assert "fetch" not in stages, "fetch must be commented out, not an active stage"
    assert "#  fetch:" in raw_text, "commented fetch block must be present"

    # Schema generation: stata keeps a dedicated `schema` stage; py/r fold the
    # schema output into validate. Either way the DAG stays acyclic.
    if language == "stata":
        assert stages["schema"]["outs"] == ["../schemas/v1/schema.json"]
        assert "../schemas/v1/schema.json" not in validate.get("outs", [])
    else:
        assert "schema" not in stages
        assert "../schemas/v1/schema.json" in validate["outs"]


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_validate_stubs_no_stale_clean_reference(
    tmp_path: Path, language: str
) -> None:
    """Rendered validate scripts must point users at `dvc repro ingest`, not the
    demoted clean stage."""
    render_scaffold(
        project_type="data", name="foo", language=language, target_dir=tmp_path
    )
    ext = {"python": "py", "r": "R", "stata": "do"}[language]
    validate_script = (tmp_path / "code" / f"validate.{ext}").read_text(
        encoding="utf-8"
    )
    assert "Run clean." not in validate_script, (
        f"validate.{ext} must not reference the demoted clean stage"
    )
    assert "dvc repro ingest" in validate_script, (
        f"validate.{ext} empty-dir message must point to `dvc repro ingest`"
    )


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_data_scaffold_has_no_clean_stub_reference(
    tmp_path: Path, language: str
) -> None:
    """The `clean.*` stub was deleted; no rendered data-scaffold file may point
    back at it. Generalizes the per-file validate check to the whole tree, so a
    reintroduced pointer to the removed stub is caught anywhere it lands."""
    import re

    written = render_scaffold(
        project_type="data", name="foo", language=language, target_dir=tmp_path
    )
    pattern = re.compile(r"clean\.(py|R|do)")
    offenders = [
        p.relative_to(tmp_path).as_posix()
        for p in written
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{language}: rendered files still reference the deleted clean stub: "
        f"{offenders}"
    )


def test_run_all_do_fails_loud_on_missing_config(tmp_path: Path) -> None:
    """F5: run_all.do must guard `code/config.do` with `capture confirm file`
    + `exit 601`, so a missing config names the path instead of failing with a
    bare `r(601)`. A revert to a silent/unguarded load leaves this red."""
    render_scaffold(
        project_type="project", name="foo", language="stata", target_dir=tmp_path
    )
    run_all = (tmp_path / "run_all.do").read_text(encoding="utf-8")
    assert "capture confirm file" in run_all, (
        "run_all.do must probe for config with `capture confirm file`"
    )
    assert "exit 601" in run_all, (
        "run_all.do must `exit 601` on a missing config, not fall through"
    )
    assert 'do "`project_root\'/code/00_setup/config.do"' not in run_all, (
        "run_all.do must not reference the phantom code/00_setup/ dir"
    )


def test_run_all_r_fails_loud_on_missing_config(tmp_path: Path) -> None:
    """F5-R: run_all.R must `stop()` when `code/config.R` is missing rather than
    silently skipping the config load. A revert to the old
    `if (file.exists(config_path))` swallow leaves this red."""
    render_scaffold(
        project_type="project", name="foo", language="r", target_dir=tmp_path
    )
    run_all = (tmp_path / "run_all.R").read_text(encoding="utf-8")
    assert 'stop("Configuration not found' in run_all, (
        "run_all.R must stop() when config.R is missing"
    )
    assert '"00_setup"' not in run_all, (
        "run_all.R must not reference the phantom code/00_setup/ dir"
    )


# --- correctness invariants -----------------------------------------------

def test_rendered_metadata_round_trips_through_pydantic(tmp_path: Path) -> None:
    render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    metadata = Metadata.model_validate_json((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata.project.name == "foo"
    assert metadata.project.full_name == "data_foo"
    assert metadata.project.type == "data"


def test_no_unrendered_jinja_markers_in_output(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text, f"unrendered marker in {path.name}: {text!r}"
        assert "{%" not in text, f"unrendered block in {path.name}: {text!r}"


@pytest.mark.parametrize("project_type", ["data", "project", "code", "enclave"])
@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_render_scaffold_matrix_no_unrendered_markers(
    tmp_path: Path, project_type: str, language: str
) -> None:
    """Render every (project_type, language) combination and assert that no
    template produces unrendered Jinja markers. Catches a `.j2` file with a
    typo or an undefined-variable reference in a rarely-exercised combo
    (e.g., project/r, project/stata) that the happy-path tests don't touch.
    """
    written = render_scaffold(
        project_type=project_type, name="foo", language=language, target_dir=tmp_path
    )
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text, f"{project_type}/{language}: unrendered marker in {path.name}"
        assert "{%" not in text, f"{project_type}/{language}: unrendered block in {path.name}"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX exec bit; the scaffolded .sh are bash-only (Windows GA "
    "tracks .ps1 siblings — see project_windows_support_followup)",
)
def test_enclave_shell_scripts_executable(tmp_path: Path) -> None:
    """Enclave transfer shell scripts must ship with 0o755. The legacy
    workflow `bash scripts/pull_data.sh` relies on the exec bit."""
    render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    for rel in (
        "scripts/pull_data.sh",
        "scripts/package_transfer.sh",
        "scripts/unpack_transfer.sh",
        "scripts/verify_transfer.sh",
    ):
        mode = (tmp_path / rel).stat().st_mode
        assert mode & 0o111, f"{rel} is not executable (mode {oct(mode)})"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX exec bit; the scaffolded .sh are bash-only (Windows GA "
    "tracks .ps1 siblings — see project_windows_support_followup)",
)
def test_data_scaffold_check_scripts_executable(tmp_path: Path) -> None:
    """The data scaffold's pre-commit-driven check scripts must also be exec."""
    render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    for rel in ("scripts/check-dvc-sync.sh", "scripts/check-env-lockfiles.sh"):
        mode = (tmp_path / rel).stat().st_mode
        assert mode & 0o111, f"{rel} is not executable (mode {oct(mode)})"


def test_scaffold_template_names_resolve_to_vendored_files() -> None:
    """Every template_name returned by every scaffold must exist on disk."""
    from importlib.resources import files
    available = {p.name for p in (files("mintd") / "files").iterdir()}
    for project_type in ("data", "project", "code", "enclave"):
        for language in ("python", "r", "stata"):
            _, file_list = dispatch(project_type)(language, "foo", f"{project_type}_foo")
            for _, template_name in file_list:
                assert template_name in available, (
                    f"{project_type}/{language}: missing template {template_name}"
                )


def test_gitignore_negates_dvc_pointers_under_downloads(tmp_path: Path) -> None:
    """The scaffolded `.gitignore` must keep bulk data under `downloads/`
    ignored while leaving DVC `.dvc` pointers trackable — else `dvc import`
    refuses to write into the enclave staging tree (slice 47, Failure 2)."""
    import subprocess

    from tests.scaffold_contract import is_ignored

    render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert not is_ignored(tmp_path, "downloads/provider-x/_staging/outputs.dvc")
    assert is_ignored(tmp_path, "downloads/provider-x/_staging/outputs/part.parquet")


@pytest.mark.parametrize("template_name", ["transfer.py.j2", "package.py.j2"])
def test_transfer_manifest_default_schema_version_validates(
    template_name: str, tmp_path: Path
) -> None:
    """A transfer manifest built from a source manifest that omits
    ``schema_version`` must default to ``"2.0"`` and validate against
    ``TransferManifest`` (slice 46). Guards the fallback default in the
    generated transfer/package scripts against future drift back to ``"1.0"``.
    """
    from mintd.enclave import TransferManifest

    source = render_template(template_name, _MIN_CONTEXT)
    namespace: dict[str, object] = {"__file__": str(tmp_path / "script.py")}
    exec(compile(source, template_name, "exec"), namespace)

    # Simulate an enclave manifest missing ``schema_version`` (hand-edited,
    # older file, or partial write) with no downloaded contents.
    namespace["load_manifest"] = lambda: {"enclave_name": "foo"}

    manifest = namespace["create_transfer_manifest"]({})

    assert manifest["schema_version"] == "2.0"
    TransferManifest.model_validate(manifest)


# --- helpers --------------------------------------------------------------

_MIN_CONTEXT: dict[str, object] = {
    "project_name": "foo",
    "full_project_name": "data_foo",
    "package_name": "foo",
    "project_type": "data",
    "language": "python",
    "language_version": "",
    "source_dir": "code",
    "created_at": "2026-01-01T00:00:00+00:00",
    "created_by": "tester",
    "author": "",
    "organization": "",
    "mint_version": "0.0.1",
    "mint_hash": "deadbeef",
    "platform_os": "linux",
    "command_sep": "&&",
    "stata_executable": "stata",
    "classification": "private",
    "contract_info": "",
    "description": "",
    "tags": [],
    "data_kind": "",
    "team": "",
    "admin_team": "",
    "researcher_team": "",
    "bucket_name": "",
    "storage_endpoint": "",
    "storage_prefix": "",
    "storage_provider": "s3",
    "storage_versioning": True,
    "dvc_remote_name": "origin",
    "registry_org": "",
    "registry_url": "",
    "mirror_url": "",
    "mirror_purpose": "",
    "data_products_primary": "",
}
