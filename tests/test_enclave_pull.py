import pytest
from pathlib import Path
from datetime import date, datetime
from mintd._dvc_ops import DvcPullError
from mintd.enclave import enclave_pull, ApprovedProduct, DownloadedItem, EnclaveManifest, ImportNotFound

class _Client:
    def fetch(self, name):
        class Entry:
            repo_url = "http://fake"
        return Entry()

class _FakeDvcOps:
    def __init__(self):
        self.calls = []
        self.init_calls = []
    def init(self, *, cwd=None):
        self.init_calls.append(cwd)
    def import_(self, repo_url, path, dest, rev, force, extra_args=None):
        self.calls.append((repo_url, path, dest, rev, force))
        # Mirror real `dvc import`: the stage working dir must already exist.
        # enclave_pull is responsible for creating it (slice 47); don't mkdir
        # here, or we'd mask a regression of that fix.
        assert dest.parent.exists(), f"stage working dir {dest.parent} does not exist"
        dest.write_text("dummy-data")
        dvc_path = dest.parent / (dest.name + ".dvc")
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


def test_enclave_pull_updates_status_per_producer(tmp_path):
    """Slice 38a: enclave_pull fires one update_status per producer with an
    (i/N) suffix, in order, BEFORE the idempotence skip (so N == #producers)."""
    class _RecordingReporter:
        def __init__(self):
            self.labels = []
        def update_status(self, msg):
            self.labels.append(msg)

    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
        ApprovedProduct(repo="b", registry_entry="e", pin="1"),
        ApprovedProduct(repo="c", registry_entry="e", pin="1"),
    ]).save(m_path)
    dvc = _FakeDvcOps()

    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()

    rep = _RecordingReporter()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory, reporter=rep)
    assert len(rep.labels) == 3
    assert "(1/3)" in rep.labels[0]
    assert "(2/3)" in rep.labels[1]
    assert "(3/3)" in rep.labels[2]


def test_enclave_pull_wraps_dvc_error_as_enclave_pull_error(tmp_path):
    """A failing dvc_ops.import_ surfaces as EnclavePullError carrying .repo."""
    from mintd._dvc_ops import DvcPullError
    from mintd.enclave import EnclavePullError

    class _FailingDvcOps:
        def init(self, *, cwd=None):
            pass
        def import_(self, repo_url, path, dest, rev, force, extra_args=None):
            raise DvcPullError("network down")

    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="repo-b", registry_entry="e", pin="1"),
    ]).save(m_path)

    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()

    with pytest.raises(EnclavePullError) as ei:
        enclave_pull(_Client(), _FailingDvcOps(), manifest_path=m_path, producer_view_factory=factory)
    assert ei.value.repo == "repo-b"


# Slice 47 — lazy `dvc init` so a fresh enclave (no `.dvc/`) pulls without a
# manual `dvc init`.

def _single_out_factory(url, pin):
    class View:
        def primary_or_raise(self): return "out"
    return View()


def test_pull_lazy_inits_dvc_when_no_dvc_dir(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)
    dvc = _FakeDvcOps()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=_single_out_factory)
    assert dvc.init_calls == [m_path.parent]


def test_pull_skips_init_when_dvc_dir_exists(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)
    (tmp_path / ".dvc").mkdir()
    dvc = _FakeDvcOps()
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=_single_out_factory)
    assert dvc.init_calls == []


class _PartialFailDvc:
    """dvc_ops double that fetches every repo except `fail_repo`, which raises.

    Derives the repo from the dest layout (downloads/<repo>/_staging/<name>) so
    no catalog wiring is needed. Mirrors _FakeDvcOps, including the
    stage-working-dir existence guard.
    """

    def __init__(self, fail_repo):
        self.fail_repo = fail_repo
        self.calls = []

    def init(self, *, cwd=None):
        pass

    def import_(self, repo_url, path, dest, rev, force, extra_args=None):
        self.calls.append((repo_url, path, dest, rev, force))
        repo = dest.parent.parent.name
        if repo == self.fail_repo:
            raise DvcPullError("boom")
        assert dest.parent.exists(), f"stage working dir {dest.parent} does not exist"
        dest.write_text("dummy-data")
        dvc_path = dest.parent / (dest.name + ".dvc")
        dvc_path.write_text("outs:\n- md5: ffffffffffffffffffffffffffffffff\n")
        return dvc_path


def test_pull_partial_run_persists_completed_products(tmp_path):
    """Defect 1: a later producer's failure must not discard the earlier
    products' downloaded[] rows. The completed product is persisted and a
    re-run skips it — while the bad producer still aborts loudly."""
    from mintd.enclave import EnclavePullError

    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="good", registry_entry="e", pin="1", source_path="ok"),
        ApprovedProduct(repo="bad", registry_entry="e", pin="1", source_path="boom"),
    ]).save(m_path)
    downloads = tmp_path / "downloads"

    # Run 1: aborts on `bad`, but `good` was already fetched+moved.
    dvc1 = _PartialFailDvc("bad")
    with pytest.raises(EnclavePullError) as ei:
        enclave_pull(_Client(), dvc1, manifest_path=m_path,
                     downloads_root=downloads, today=date(2026, 5, 20))
    assert ei.value.repo == "bad"
    m1 = EnclaveManifest.load(m_path)
    assert [d.repo for d in m1.downloaded] == ["good"]

    # Run 2: `good` is fast-skipped (not re-imported); `bad` still aborts.
    dvc2 = _PartialFailDvc("bad")
    with pytest.raises(EnclavePullError):
        enclave_pull(_Client(), dvc2, manifest_path=m_path,
                     downloads_root=downloads, today=date(2026, 5, 20))
    imported_repos = [Path(c[2]).parent.parent.name for c in dvc2.calls]
    assert imported_repos == ["bad"]  # `good` skipped, only `bad` attempted
    assert [d.repo for d in EnclaveManifest.load(m_path).downloaded] == ["good"]


def test_all_already_downloaded_fires_for_primary(tmp_path):
    """Defect 2: a recorded primary product (stored output is a RESOLVED path,
    not the dead 'primary' sentinel) fast-skips on re-run with no catalog fetch
    and no dvc import."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
    ], downloaded=[
        DownloadedItem(repo="a", output="data/final/", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
    ]).save(m_path)

    class _NoFetchClient:
        def fetch(self, name):
            raise AssertionError("catalog fetch must not happen on re-run")

    def factory(url, pin):
        raise AssertionError("producer resolve must not happen on re-run")

    dvc = _FakeDvcOps()
    _, written = enclave_pull(_NoFetchClient(), dvc, manifest_path=m_path,
                              producer_view_factory=factory)
    assert dvc.calls == []
    assert written == []


def test_pull_force_failure_preserves_failing_products_row(tmp_path):
    """Under --force a row is pruned+re-appended atomically on success; if a
    product's re-import FAILS, its pre-existing downloaded[] row must survive
    the failure flush (not be dropped, which would orphan its on-disk data)."""
    from mintd.enclave import EnclavePullError

    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="good", registry_entry="e", pin="1", source_path="ok"),
        ApprovedProduct(repo="bad", registry_entry="e", pin="1", source_path="boom"),
    ], downloaded=[
        DownloadedItem(repo="good", output="ok", contract_pin="1", artifact_pin="oldg",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lpg"),
        DownloadedItem(repo="bad", output="boom", contract_pin="1", artifact_pin="oldb",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lpb"),
    ]).save(m_path)
    downloads = tmp_path / "downloads"

    dvc = _PartialFailDvc("bad")
    with pytest.raises(EnclavePullError) as ei:
        enclave_pull(_Client(), dvc, manifest_path=m_path, force=True,
                     downloads_root=downloads, today=date(2026, 5, 20))
    assert ei.value.repo == "bad"

    m = EnclaveManifest.load(m_path)
    # `bad`'s pre-existing row survives (import failed, prune never ran); `good`
    # was re-imported so its row is replaced (not duplicated).
    assert sorted(d.repo for d in m.downloaded) == ["bad", "good"]
    bad_rows = [d for d in m.downloaded if d.repo == "bad"]
    assert len(bad_rows) == 1 and bad_rows[0].artifact_pin == "oldb"
    good_rows = [d for d in m.downloaded if d.repo == "good"]
    assert len(good_rows) == 1 and good_rows[0].artifact_pin == "f" * 32


class _FailOnOutputDvc:
    """dvc_ops double that fails on one specific output path."""

    def __init__(self, fail_output):
        self.fail_output = fail_output
        self.calls = []

    def init(self, *, cwd=None):
        pass

    def import_(self, repo_url, path, dest, rev, force, extra_args=None):
        self.calls.append(path)
        if path == self.fail_output:
            raise DvcPullError("boom")
        assert dest.parent.exists(), f"stage working dir {dest.parent} does not exist"
        dest.write_text("dummy-data")
        dvc_path = dest.parent / (dest.name + ".dvc")
        dvc_path.write_text("outs:\n- md5: ffffffffffffffffffffffffffffffff\n")
        return dvc_path


def test_pull_all_product_partial_outputs_persist_before_failure(tmp_path):
    """Defect 1 (intra-`all`): when a multi-output `all` product fails on a
    later output, the earlier outputs already fetched are persisted before the
    abort, and a re-run inner-skips them and retries only the missing output."""
    from mintd.enclave import EnclavePullError

    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True),
    ]).save(m_path)
    downloads = tmp_path / "downloads"

    def factory(url, pin):
        class View:
            def output_paths(self): return ["o1", "o2"]
        return View()

    # Run 1: o1 succeeds, o2 aborts. o1's row must persist despite the abort.
    dvc1 = _FailOnOutputDvc("o2")
    with pytest.raises(EnclavePullError):
        enclave_pull(_Client(), dvc1, manifest_path=m_path, downloads_root=downloads,
                     producer_view_factory=factory, today=date(2026, 5, 20))
    m = EnclaveManifest.load(m_path)
    assert [d.output for d in m.downloaded] == ["o1"]

    # Run 2: o1 inner-skipped (already downloaded), only o2 retried.
    dvc2 = _FailOnOutputDvc("o2")
    with pytest.raises(EnclavePullError):
        enclave_pull(_Client(), dvc2, manifest_path=m_path, downloads_root=downloads,
                     producer_view_factory=factory, today=date(2026, 5, 20))
    assert dvc2.calls == ["o2"]
