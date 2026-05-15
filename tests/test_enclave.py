import pytest
from pathlib import Path
from datetime import date
from pydantic import ValidationError
from mintd.enclave import EnclaveManifest, TransferredItem, AppendOnlyViolation
from mintd.data import ImportNotFound

FIXTURE = Path(__file__).parent / "fixtures" / "enclave_manifest_v2_minimal.yaml"

def test_load_minimal_manifest_round_trips():
    m = EnclaveManifest.load(FIXTURE)
    reloaded = EnclaveManifest.model_validate(m.model_dump(mode="json"))
    assert m == reloaded

def test_load_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        EnclaveManifest.load(tmp_path / "missing.yaml")

def test_load_invalid_yaml_raises_validation_error(tmp_path):
    p = tmp_path / "invalid.yaml"
    p.write_text("schema_version: 1.0\nenclave_name: x\n")
    with pytest.raises(ValidationError):
        EnclaveManifest.load(p)

def test_save_creates_file_at_path(tmp_path):
    m = EnclaveManifest(enclave_name="test")
    p = tmp_path / "out.yaml"
    m.save(p)
    assert p.exists()
    assert EnclaveManifest.load(p).enclave_name == "test"

def test_save_missing_parent_dir_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        EnclaveManifest(enclave_name="test").save(tmp_path / "nonexistent" / "out.yaml")

def test_save_appends_transferred_entries_cleanly(tmp_path):
    m = EnclaveManifest.load(FIXTURE)
    new_m = m.model_copy(update={"transferred": [
        TransferredItem(repo="r", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp")
    ]})
    new_m.save(tmp_path / "appended.yaml")
    reloaded = EnclaveManifest.load(tmp_path / "appended.yaml")
    assert len(reloaded.transferred) == 1

def test_save_modified_transferred_entry_raises_append_only_violation(tmp_path):
    m = EnclaveManifest.load(FIXTURE)
    m = m.model_copy(update={"transferred": [
        TransferredItem(repo="r", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp")
    ]})
    m.save(tmp_path / "base.yaml")
    
    tampered = m.model_copy(update={"transferred": [
        TransferredItem(repo="r", contract_pin="MODIFIED", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp")
    ]})
    with pytest.raises(AppendOnlyViolation) as exc:
        tampered.save(tmp_path / "base.yaml")
    assert exc.value.changed_indices == [0]

def test_save_removed_transferred_entry_raises_append_only_violation(tmp_path):
    m = EnclaveManifest.load(FIXTURE)
    m = m.model_copy(update={"transferred": [
        TransferredItem(repo="r1", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp1"),
        TransferredItem(repo="r2", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t2", local_path="lp2")
    ]})
    m.save(tmp_path / "base.yaml")
    
    tampered = m.model_copy(update={"transferred": [
        TransferredItem(repo="r1", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp1")
    ]})
    with pytest.raises(AppendOnlyViolation) as exc:
        tampered.save(tmp_path / "base.yaml")
    assert exc.value.changed_indices == [1]

def test_save_modify_and_remove_reports_all_changed_indices(tmp_path):
    """Modify entry 0 *and* remove entry 2 → changed_indices == [0, 2].

    Pins the slice-8-retro P2 fix: the diff must enumerate both modifications
    in the overlap *and* tail removals, not stop at the first kind.
    """
    m = EnclaveManifest.load(FIXTURE)
    items = [
        TransferredItem(repo=f"r{i}", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id=f"t{i}", local_path=f"lp{i}")
        for i in range(3)
    ]
    m = m.model_copy(update={"transferred": items})
    m.save(tmp_path / "base.yaml")

    tampered_first = items[0].model_copy(update={"contract_pin": "TAMPERED"})
    tampered = m.model_copy(update={"transferred": [tampered_first, items[1]]})
    with pytest.raises(AppendOnlyViolation) as exc:
        tampered.save(tmp_path / "base.yaml")
    assert exc.value.changed_indices == [0, 2]


def test_save_reordered_transferred_raises_append_only_violation(tmp_path):
    m = EnclaveManifest.load(FIXTURE)
    m = m.model_copy(update={"transferred": [
        TransferredItem(repo="r1", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp1"),
        TransferredItem(repo="r2", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t2", local_path="lp2")
    ]})
    m.save(tmp_path / "base.yaml")
    
    tampered = m.model_copy(update={"transferred": [
        TransferredItem(repo="r2", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t2", local_path="lp2"),
        TransferredItem(repo="r1", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp1")
    ]})
    with pytest.raises(AppendOnlyViolation) as exc:
        tampered.save(tmp_path / "base.yaml")
    assert exc.value.changed_indices == [0, 1]

def test_apply_pin_bump_returns_new_manifest_with_updated_pin():
    m = EnclaveManifest.load(FIXTURE)
    bumped = m.apply_pin_bump(repo="provider-xw", new_pin="NEW_PIN")
    assert bumped.approved_products[0].pin == "NEW_PIN"
    assert m.approved_products[0].pin != "NEW_PIN"

def test_apply_pin_bump_unknown_repo_raises_import_not_found():
    m = EnclaveManifest.load(FIXTURE)
    with pytest.raises(ImportNotFound):
        m.apply_pin_bump(repo="unknown", new_pin="X")

def test_transferred_item_is_frozen():
    item = TransferredItem(repo="r", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp")
    with pytest.raises(ValidationError):
        item.repo = "MODIFIED"
