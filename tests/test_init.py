from __future__ import annotations

from pathlib import Path

import pytest

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
    Metadata.model_validate_json(metadata_path.read_text())


def test_init_writes_gitignore(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    project_path, _ = init_project(
        project_type="data", name="my_proj", target_dir=tmp_path, ops=fake
    )
    gitignore_path = project_path / ".gitignore"
    assert gitignore_path.exists()
    # The vendored .gitignore is the legacy one; just confirm non-empty.
    assert gitignore_path.read_text().strip()


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
    assert metadata_path.read_text() == "{}"
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
        (project_path / "metadata.json").read_text()
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
    assert (project_path / "code" / "clean.py").exists()
    assert (project_path / "code" / "validate.py").exists()
    assert (project_path / "schemas" / "generate_schema.py").exists()


def test_init_invalid_name_raises(tmp_path: Path) -> None:
    fake = _FakeInitOps()
    with pytest.raises(InitNameInvalid):
        init_project(
            project_type="data", name="-bad", target_dir=tmp_path, ops=fake
        )
