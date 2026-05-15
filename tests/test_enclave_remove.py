import pytest
from datetime import datetime
from mintd.enclave import enclave_remove, ApprovedProduct, DownloadedItem, EnclaveManifest, ImportNotFound

class _Client:
    def fetch(self, name):
        class Entry:
            repo_url = "http://fake"
        return Entry()

def test_remove_clears_approved_products(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
        ApprovedProduct(repo="b", registry_entry="e", pin="2")
    ]).save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a")
    m = EnclaveManifest.load(m_path)
    assert len(m.approved_products) == 1
    assert m.approved_products[0].repo == "b"

def test_remove_clears_downloaded_too(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ], downloaded=[
        DownloadedItem(repo="a", output="out", contract_pin="1", artifact_pin="p", 
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp")
    ]).save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a")
    m = EnclaveManifest.load(m_path)
    assert len(m.approved_products) == 0
    assert len(m.downloaded) == 0

def test_remove_preserves_transferred(tmp_path):
    from mintd.enclave import TransferredItem
    from datetime import date
    m_path = tmp_path / "enclave_manifest.yaml"
    orig = EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ], transferred=[
        TransferredItem(repo="a", contract_pin="1", artifact_pin="p", transfer_date=date(2026,5,20), 
                       transfer_id="1", local_path="lp")
    ])
    orig.save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a")
    m = EnclaveManifest.load(m_path)
    assert m.transferred[0].model_dump() == orig.transferred[0].model_dump()

def test_remove_source_path_filter_keeps_other_entries(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="path1"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="path2")
    ]).save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a", source_path="path1")
    m = EnclaveManifest.load(m_path)
    assert len(m.approved_products) == 1
    assert m.approved_products[0].source_path == "path2"

def test_remove_wipes_downloads_dir_when_last_subscription(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    d_root = tmp_path / "downloads"
    r_dir = d_root / "a"
    r_dir.mkdir(parents=True)
    (r_dir / "data.txt").write_text("hello")
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a", downloads_root=d_root)
    assert not r_dir.exists()

def test_remove_source_path_preserves_other_downloads(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    d_root = tmp_path / "downloads"
    r_dir = d_root / "a"
    r_dir.mkdir(parents=True)
    (r_dir / "data.csv").write_text("hello")
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="p1"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="p2")
    ]).save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a", source_path="p1", downloads_root=d_root)
    assert r_dir.exists()
    assert (r_dir / "data.csv").exists()

def test_remove_no_downloads_dir_no_error(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)
    enclave_remove(_Client(), manifest_path=m_path, name="a", downloads_root=tmp_path / "ghost")
    assert not (tmp_path / "ghost").exists()

def test_remove_unknown_repo_raises_import_not_found(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="other", registry_entry="e", pin="1")
    ]).save(m_path)
    with pytest.raises(ImportNotFound):
        enclave_remove(_Client(), manifest_path=m_path, name="ghost")


def test_remove_source_path_preserves_repo_downloads_dir(tmp_path):
    """Regression: enclave_remove --source-path was wiping the entire repo's
    downloads/ dir even when sibling outputs still referenced it."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="x"),
            ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="y"),
        ],
        downloaded=[
            DownloadedItem(
                repo="a", output="y", contract_pin="1", artifact_pin="kept",
                fetch_strategy="dvc-import", downloaded_at=datetime(2026, 1, 1),
                local_path="downloads/a/kept-2026-01-01",
            ),
        ],
    ).save(m_path)
    downloads_dir = tmp_path / "downloads" / "a"
    downloads_dir.mkdir(parents=True)
    (downloads_dir / "marker").write_text("preserved")

    enclave_remove(
        _Client(), manifest_path=m_path, name="a",
        source_path="x", downloads_root=tmp_path / "downloads",
    )

    # Output "y" still references downloads/a; the wipe must leave it intact.
    assert (downloads_dir / "marker").exists()
