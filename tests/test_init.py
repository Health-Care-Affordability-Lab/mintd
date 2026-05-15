import pytest
from pathlib import Path
from mintd.init import init_project, InitDestinationExists
from mintd.model import Metadata
from tests._fakes.init_ops import _FakeInitOps


def test_init_writes_metadata_json(tmp_path: Path):
    target_dir = tmp_path / "proj"
    fake = _FakeInitOps()
    init_project(project_type="data", name="my_proj", target_dir=target_dir, ops=fake)
    
    metadata_path = target_dir / "metadata.json"
    assert metadata_path.exists()
    Metadata.model_validate_json(metadata_path.read_text())


def test_init_writes_gitignore(tmp_path: Path):
    target_dir = tmp_path / "proj"
    fake = _FakeInitOps()
    init_project(project_type="data", name="my_proj", target_dir=target_dir, ops=fake)
    
    gitignore_path = target_dir / ".gitignore"
    assert gitignore_path.exists()
    assert "__pycache__/" in gitignore_path.read_text()


def test_init_runs_git_init(tmp_path: Path):
    target_dir = tmp_path / "proj"
    fake = _FakeInitOps()
    init_project(project_type="data", name="my_proj", target_dir=target_dir, ops=fake)
    
    assert fake.git_calls == [target_dir]


@pytest.mark.parametrize("ptype", ["data", "code", "project"])
def test_init_runs_dvc_init_for_valid_types(tmp_path: Path, ptype):
    target_dir = tmp_path / f"proj_{ptype}"
    fake = _FakeInitOps()
    init_project(project_type=ptype, name="my_proj", target_dir=target_dir, ops=fake)
    
    assert fake.dvc_calls == [target_dir]


def test_init_skips_dvc_init_for_enclave_type(tmp_path: Path):
    target_dir = tmp_path / "proj"
    fake = _FakeInitOps()
    init_project(project_type="enclave", name="my_proj", target_dir=target_dir, ops=fake)
    
    assert fake.dvc_calls == []
    assert fake.git_calls == [target_dir]


def test_init_existing_metadata_raises(tmp_path: Path):
    target_dir = tmp_path / "proj"
    target_dir.mkdir()
    metadata_path = target_dir / "metadata.json"
    metadata_path.write_text("{}")
    
    fake = _FakeInitOps()
    with pytest.raises(InitDestinationExists):
        init_project(project_type="data", name="my_proj", target_dir=target_dir, ops=fake)
    
    assert metadata_path.read_text() == "{}"
    assert fake.git_calls == []
    assert fake.dvc_calls == []


def test_init_creates_target_dir_if_missing(tmp_path: Path):
    target_dir = tmp_path / "new" / "nested"
    fake = _FakeInitOps()
    init_project(project_type="data", name="my_proj", target_dir=target_dir, ops=fake)
    
    assert target_dir.exists()


def test_init_metadata_includes_passed_name_and_type(tmp_path: Path):
    target_dir = tmp_path / "proj"
    fake = _FakeInitOps()
    init_project(project_type="data", name="my_proj", target_dir=target_dir, ops=fake)
    
    metadata = Metadata.model_validate_json((target_dir / "metadata.json").read_text())
    assert metadata.project.name == "my_proj"
    assert metadata.project.type == "data"
    assert metadata.project.full_name == "data_my_proj"
