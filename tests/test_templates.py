"""Tests for the Jinja-rendered scaffold engine (slice 19)."""

from __future__ import annotations

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
    assert "code/ingest.py" in rel
    assert "code/clean.py" in rel
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
    assert "code/ingest.R" in rel
    assert "code/clean.R" in rel
    assert "DESCRIPTION" in rel
    assert "renv.lock" in rel
    assert "code/ingest.py" not in rel


def test_data_stata_renders_do_sources(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="stata", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "code/ingest.do" in rel
    assert "code/clean.do" in rel
    assert "stata-packages.txt" in rel
    assert "code/ingest.py" not in rel


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
    "methods": "",
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
    "data_dependencies": [],
    "data_products_primary": "",
    "configurations": [],
}
