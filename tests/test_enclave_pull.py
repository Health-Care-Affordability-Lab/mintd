import pytest
from pathlib import Path
from datetime import date, datetime
from mintd.enclave import enclave_pull, ApprovedProduct, DownloadedItem, EnclaveManifest, ImportNotFound, ProducerView

class _Client:
    def fetch(self, name):
        class Entry:
            repo_url = "http://fake"
        return Entry()

class _FakeDvcOps:
    def __init__(self):
        self.calls = []
    def import_(self, repo_url, path, dest, rev, force):
        self.calls.append((repo_url, path, dest, rev, force))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("dummy-data")  # Add this: create the file
        dvc_path = dest.parent / (dest.name + ".dvc")
        dvc_path.parent.mkdir(parents=True, exist_ok=True)
        dvc_path.write_text("outs:\n- md5: ffffffffffffffffffffffffffffffff\n")
        return dvc_path

def test_pull_single_repo_writes_downloaded(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)
    m = EnclaveManifest.load(m_path)
    assert len(m.downloaded) == 1
    d = m.downloaded[0]
    assert d.repo == "a"
    assert d.output == "out"
    assert d.contract_pin == "1"
    assert d.artifact_pin == "f" * 32
    assert d.fetch_strategy == "dvc-import"

def test_pull_all_repos_walks_each(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
        ApprovedProduct(repo="b", registry_entry="e", pin="1"),
        ApprovedProduct(repo="c", registry_entry="e", pin="1")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)
    assert len(dvc.calls) == 3

def test_pull_source_path_override(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="manual")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        raise AssertionError("should not be called")
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)
    m = EnclaveManifest.load(m_path)
    assert m.downloaded[0].output == "manual"

def test_pull_all_outputs_walks_view_outputs(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True)
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def output_paths(self): return ["o1", "o2", "o3"]
        return View()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)
    m = EnclaveManifest.load(m_path)
    assert len(m.downloaded) == 3

def test_pull_idempotent_skips_existing(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="out")
    ], downloaded=[
        DownloadedItem(repo="a", output="out", contract_pin="1", artifact_pin="p", 
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin): raise AssertionError("factory should not be called")
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)
    assert len(dvc.calls) == 0

def test_pull_force_re_downloads(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ], downloaded=[
        DownloadedItem(repo="a", output="out", contract_pin="1", artifact_pin="p", 
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()
    enclave_pull(_Client(), dvc, manifest_path=m_path, force=True, producer_view_factory=factory)
    assert len(dvc.calls) == 1

def test_pull_force_replaces_existing_entry(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ], downloaded=[
        DownloadedItem(repo="a", output="out", contract_pin="1", artifact_pin="old", 
                       fetch_strategy="dvc-import", downloaded_at=datetime(2025,1,1), local_path="old")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()
    enclave_pull(_Client(), dvc, manifest_path=m_path, force=True, producer_view_factory=factory)
    enclave_pull(_Client(), dvc, manifest_path=m_path, force=True, producer_view_factory=factory)
    m = EnclaveManifest.load(m_path)
    assert len(m.downloaded) == 1
    assert m.downloaded[0].artifact_pin == "f" * 32

def test_pull_filesystem_layout(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()
    _, written = enclave_pull(_Client(), dvc, manifest_path=m_path, 
                              producer_view_factory=factory, today=date(2026, 5, 20))
    path = Path(written[0].local_path)
    assert path.name.startswith("fffffff-2026-05-20")
    assert path.parent.name == "a"

def test_pull_preserves_transferred(tmp_path):
    from mintd.enclave import TransferredItem
    m_path = tmp_path / "enclave_manifest.yaml"
    orig = EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ], transferred=[
        TransferredItem(repo="a", contract_pin="1", artifact_pin="p", transfer_date=date(2026,5,20), 
                       transfer_id="1", local_path="lp")
    ])
    orig.save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)
    m = EnclaveManifest.load(m_path)
    assert m.transferred[0].model_dump() == orig.transferred[0].model_dump()

def test_pull_unknown_repo_raises_import_not_found(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="other", registry_entry="e", pin="1")
    ]).save(m_path)
    with pytest.raises(ImportNotFound):
        enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path, repo="ghost")


def test_pull_force_does_not_duplicate_downloaded_entries(tmp_path):
    """Regression: --force previously appended a new DownloadedItem without
    removing the matching existing entry, growing the manifest by one row
    per pull."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="out"),
        ],
        downloaded=[
            DownloadedItem(
                repo="a", output="out", contract_pin="1", artifact_pin="old",
                fetch_strategy="dvc-import", downloaded_at=datetime(2025, 1, 1),
                local_path="downloads/a/old-2025-01-01",
            ),
        ],
    ).save(m_path)
    dvc = _FakeDvcOps()
    enclave_pull(
        _Client(), dvc, manifest_path=m_path,
        downloads_root=tmp_path / "downloads", force=True, today=date(2026, 5, 20),
    )
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.downloaded) == 1
    assert reloaded.downloaded[0].artifact_pin == "f" * 32


def test_pull_handles_stale_destination_from_interrupted_run(tmp_path):
    """Regression: shutil.move into an existing directory nests it instead
    of overwriting. enclave_pull must clear the final destination before
    move to recover from a previous interrupted run."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="out"),
        ],
    ).save(m_path)
    stale_dir = tmp_path / "downloads" / "a" / f"{'f' * 7}-2026-05-20" / "out"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old_file").write_text("stale data")

    dvc = _FakeDvcOps()
    enclave_pull(
        _Client(), dvc, manifest_path=m_path,
        downloads_root=tmp_path / "downloads", today=date(2026, 5, 20),
    )
    final = tmp_path / "downloads" / "a" / f"{'f' * 7}-2026-05-20" / "out"
    assert final.exists()
    # Defensive removal prevented the nested out/out structure.
    assert not (final / "out").exists()


def test_pull_clears_stale_staging_dir_from_interrupted_run(tmp_path):
    """Regression: if a prior interrupted run left files in downloads/<repo>/_staging/,
    the next pull would fail because dvc_ops.import_ would refuse to overwrite.
    enclave_pull must clear _staging at the top of each output's loop iteration."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="out"),
        ],
    ).save(m_path)
    # Pre-stage a stale _staging dir with a file at the dest location.
    staging = tmp_path / "downloads" / "a" / "_staging"
    staging.mkdir(parents=True)
    (staging / "out").write_text("stale staging data")

    dvc = _FakeDvcOps()
    _, written = enclave_pull(
        _Client(), dvc, manifest_path=m_path,
        downloads_root=tmp_path / "downloads", today=date(2026, 5, 20),
    )
    # Pull succeeded; _staging was cleared before dvc_ops.import_ wrote into it.
    assert len(written) == 1
    # The cleanup at end of loop removes _staging again.
    assert not staging.exists()
