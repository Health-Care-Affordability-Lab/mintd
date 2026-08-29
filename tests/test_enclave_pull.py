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
        # WHICH REPO each import was aimed at. Separate from `calls` so the
        # existing tuple-shape assertions keep working; unit A is what made
        # this observable at all.
        self.import_cwds = []
    def init(self, *, cwd=None):
        self.init_calls.append(cwd)
    def import_(self, repo_url, path, dest, cwd, rev, force, extra_args=None):
        self.calls.append((repo_url, path, dest, rev, force))
        self.import_cwds.append(cwd)
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
        def import_(self, repo_url, path, dest, cwd, rev, force, extra_args=None):
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

    def import_(self, repo_url, path, dest, cwd, rev, force, extra_args=None):
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

    def import_(self, repo_url, path, dest, cwd, rev, force, extra_args=None):
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


def test_enclave_pull_imports_into_the_enclave_from_an_unrelated_cwd(
    tmp_path, monkeypatch
):
    """The custody bug, pinned: `dvc import` is aimed at the enclave, never at
    whatever repo the caller happens to be standing in.

    Before unit A, `enclave_pull` pinned the working directory for `init`
    (`cwd=manifest_path.parent`) and could not pass one to `import_` at all —
    the protocol had no `cwd` on that verb. Run from anywhere but the enclave,
    a pull cached the producer's restricted bytes into the *enclosing* repo's
    `.dvc/cache` and staged dvc's bookkeeping into that repo's git index, at
    exit 0. Measured against real dvc 3.67.1 with the enclave nested one level
    down: outer cache 1 blob, enclave cache 0.

    This is the fake-backed half; `tests/test_harness_contract.py::
    test_enclave_pull_caches_into_the_enclave_not_the_outer_repo` runs the
    same claim through real dvc and checks where the bytes actually land.

    The shared `_FakeDvcOps` records `cwd` on every call tuple and is pinned
    elsewhere; this module's local double writes `dest`, which `enclave_pull`
    then moves, so it is the one that can drive the whole lane.
    """
    enclave = tmp_path / "enclave"
    enclave.mkdir()
    m_path = enclave / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1")
    ]).save(m_path)

    dvc = _FakeDvcOps()

    def factory(url, pin):
        class View:
            def primary_or_raise(self): return "out"
        return View()

    # Stand somewhere else entirely -- the shape that used to corrupt.
    monkeypatch.chdir(tmp_path)
    enclave_pull(_Client(), dvc, manifest_path=m_path, producer_view_factory=factory)

    assert dvc.import_cwds == [enclave], "dvc import was not aimed at the enclave"
    assert dvc.init_calls == [enclave]
    # init and import must agree about where they run; disagreeing is the
    # failure mode that produced the misdirecting "re-run" hint.
    assert dvc.import_cwds[0] == dvc.init_calls[0]


# --- P5: one producer, many subscriptions (issue33) -------------------------
# THE REACHABILITY TRAP. `_all_already_downloaded`'s primary fast-path keys on
# (repo, contract_pin), and its own comment used to cite "enclave_add rejects
# duplicate repos" as the proof that key is exact. P5 deletes that guard, so a
# SIBLING row's downloaded[] entry now satisfies (repo, pin) without the primary
# having been fetched -- pull exits 0 having fetched nothing. Both sibling
# shapes get their own test: a frozenset of sibling source_paths would cover
# the first and silently miss the second, because an `all` row has no path.


def _primary_factory():
    def factory(url, pin):
        class View:
            def primary_or_raise(self):
                return "data/primary/"
            def output_paths(self):
                return ["data/final/x", "data/primary/"]
        return View()
    return factory


def test_pull_primary_not_fast_skipped_by_sibling_path_row(tmp_path):
    """PULL half of P5's binding invariant: a repo subscribed to a path AND its
    primary, path fetched first, must still fetch the primary."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/final/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
    ], downloaded=[
        DownloadedItem(repo="a", output="data/final/x", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
    ]).save(m_path)

    dvc = _FakeDvcOps()
    _, written = enclave_pull(_Client(), dvc, manifest_path=m_path,
                              producer_view_factory=_primary_factory())

    assert [c[1] for c in dvc.calls] == ["data/primary/"]
    assert [d.output for d in written] == ["data/primary/"]


def test_pull_primary_not_fast_skipped_by_sibling_all_row(tmp_path):
    """The shape a sibling-source_path design misses: an `all` row has
    source_path None, so it contributes no path to discriminate on.

    The `all` row must import NOTHING here, or it would fetch the primary
    itself and the assertion would hold with the guard deleted -- i.e. the test
    would be vacuous. So its outputs are already in downloaded[], and the only
    thing that can put `data/primary/` on the wire is the primary row escaping
    the (repo, pin) fast-skip.
    """
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True),
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
    ], downloaded=[
        DownloadedItem(repo="a", output="data/final/x", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
    ]).save(m_path)

    def factory(url, pin):
        class View:
            def primary_or_raise(self):
                return "data/primary/"
            def output_paths(self):
                return ["data/final/x"]
        return View()

    dvc = _FakeDvcOps()
    _, written = enclave_pull(_Client(), dvc, manifest_path=m_path,
                              producer_view_factory=factory)

    assert [c[1] for c in dvc.calls] == ["data/primary/"]
    assert [d.output for d in written] == ["data/primary/"]


def test_pull_overlapping_rows_import_each_output_once(tmp_path):
    """N2: the inner dedup read the PRE-RUN downloaded[] snapshot, so two rows
    resolving to overlapping outputs imported the shared one twice and appended
    a duplicate provenance row to a custody manifest."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/final/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True),
    ]).save(m_path)

    dvc = _FakeDvcOps()
    _, written = enclave_pull(_Client(), dvc, manifest_path=m_path,
                              producer_view_factory=_primary_factory())

    assert [c[1] for c in dvc.calls] == ["data/final/x", "data/primary/"]
    outputs = [d.output for d in EnclaveManifest.load(m_path).downloaded]
    assert outputs == ["data/final/x", "data/primary/"]
    assert len(outputs) == len(set(outputs))


def test_pull_status_names_the_subscription(tmp_path):
    """Two rows of one repo used to render two identical status lines."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/final/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
    ]).save(m_path)

    class _RecordingReporter:
        def __init__(self):
            self.statuses = []
        def update_status(self, msg):
            self.statuses.append(msg)

    reporter = _RecordingReporter()
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path,
                 producer_view_factory=_primary_factory(), reporter=reporter)

    assert len(reporter.statuses) == 2
    assert len(set(reporter.statuses)) == 2
    assert "data/final/x" in reporter.statuses[0]
    assert "<primary>" in reporter.statuses[1]


def test_pull_force_still_imports_a_shared_output_only_once(tmp_path):
    """N2 under --force. Two rows of one repo resolving to the same output must
    not import it twice in ONE run: --force means "re-fetch across runs", never
    "fetch the same thing twice in this run", and a duplicate append lands in a
    custody manifest."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/final/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True),
    ]).save(m_path)

    dvc = _FakeDvcOps()
    enclave_pull(_Client(), dvc, manifest_path=m_path, force=True,
                 producer_view_factory=_primary_factory())

    assert [c[1] for c in dvc.calls] == ["data/final/x", "data/primary/"]
    outputs = [d.output for d in EnclaveManifest.load(m_path).downloaded]
    assert len(outputs) == len(set(outputs)), f"duplicate provenance rows: {outputs}"


# D7 (notes/mintd-check/DECISIONS-20260828.md) — fresh-clone wedge. A fresh
# clone tracks the manifest but not downloads/, so every row fast-skips off
# downloaded[] without a stat and the pull ends in a bare "nothing to pull"
# while `enclave package` then dies on the first missing dir. The fix: stat
# each in-scope downloaded[] row and REPORT the missing ones by name; NEVER
# auto-pull — a deliberate prune-after-transfer must stay pruned, and the
# manifest records no prunes, so a missing path is indistinguishable from one.

class _WarnRecorder:
    def __init__(self):
        self.warnings = []
    def update_status(self, msg):
        pass
    def warn(self, msg):
        self.warnings.append(msg)


def test_fresh_clone_reports_missing_downloads_by_name(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    gone = tmp_path / "downloads" / "acme" / "ppppppp-2026-01-01"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="pin1234abc",
                        source_path="data/final/x"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="data/final/x", contract_pin="pin1234abc",
                       artifact_pin="p", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(), local_path=str(gone)),
    ]).save(m_path)
    dvc = _FakeDvcOps()
    def factory(url, pin):
        raise AssertionError("fast-skip must not resolve the producer")
    rep = _WarnRecorder()
    _, written = enclave_pull(_Client(), dvc, manifest_path=m_path,
                              producer_view_factory=factory, reporter=rep)
    assert dvc.calls == []  # never auto-pull: a pruned file stays pruned
    assert written == []
    row_warns = [w for w in rep.warnings if str(gone) in w]
    assert len(row_warns) == 1
    assert "acme" in row_warns[0]
    assert "data/final/x" in row_warns[0]
    assert "pin1234" in row_warns[0]
    assert any("mintd enclave pull --force" in w for w in rep.warnings)


def test_missing_report_scoped_to_repo_filter(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    gone_a = tmp_path / "downloads" / "acme" / "x"
    gone_b = tmp_path / "downloads" / "beta" / "y"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="1", source_path="out-a"),
        ApprovedProduct(repo="beta", registry_entry="e", pin="1", source_path="out-b"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="out-a", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(),
                       local_path=str(gone_a)),
        DownloadedItem(repo="beta", output="out-b", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(),
                       local_path=str(gone_b)),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        raise AssertionError("fast-skip must not resolve the producer")
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path, repo="beta",
                 producer_view_factory=factory, reporter=rep)
    assert any("out-b" in w for w in rep.warnings)
    assert not any("out-a" in w for w in rep.warnings)
    assert any("mintd enclave pull --force beta" in w for w in rep.warnings)


def test_missing_report_silent_when_downloads_present(tmp_path):
    m_path = tmp_path / "enclave_manifest.yaml"
    present = tmp_path / "downloads" / "acme" / "ppppppp-2026-01-01"
    present.mkdir(parents=True)
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="1", source_path="out"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="out", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(),
                       local_path=str(present)),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        raise AssertionError("fast-skip must not resolve the producer")
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path,
                 producer_view_factory=factory, reporter=rep)
    assert rep.warnings == []


def test_force_pull_clears_superseded_pin_rows_after_bump(tmp_path):
    # Review round 1: `enclave bump` rewrites approved_products[].pin ONLY, so
    # downloaded[] keeps its row at the old contract_pin. On a fresh clone that
    # row stats missing and the D7 hint prescribes `--force` — which must
    # actually clear it (the per-repo stale-pin prune drops rows at
    # non-approved pins once the repo's imports succeed), or the warning
    # recurs on every future pull and its own remediation is a lie.
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="pin2new", source_path="out"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="out", contract_pin="pin1old",
                       artifact_pin="p", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(),
                       local_path=str(tmp_path / "downloads" / "acme" / "aaaaaaa-2026-01-01")),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        raise AssertionError("source_path is set; factory must not be called")
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path, force=True,
                 producer_view_factory=factory, reporter=rep)
    rows = [(d.output, d.contract_pin) for d in EnclaveManifest.load(m_path).downloaded]
    assert rows == [("out", "pin2new")], f"superseded row survived --force: {rows}"
    assert rep.warnings == []  # the hint's promise: --force clears the report


def test_force_pull_sibling_failure_preserves_failing_siblings_row(tmp_path):
    # Review round 3: multi-subscription repos (a supported shape as of
    # ff40b00) broke the prune's own never-drop rule. The per-repo stale-pin
    # prune fired after EACH subscription, so subscription 1's success pruned
    # sibling 2's superseded row before sibling 2 re-imported -- and when
    # sibling 2 then failed, its custody row was flushed away while its old
    # data lingered on disk. The prune now waits for the repo's LAST in-scope
    # subscription, which (failure aborts the loop) is exactly "every sibling
    # succeeded".
    #
    # Mutation: drop `i == last_of_repo[ap.repo]` from the prune guard ->
    # this test fails on the vanished pin1old row.
    from mintd._dvc_ops import DvcPullError
    from mintd.enclave import EnclavePullError

    class _SecondImportFails(_FakeDvcOps):
        def import_(self, repo_url, path, dest, cwd, rev, force, extra_args=None):
            if len(self.calls) >= 1:
                self.calls.append((repo_url, path, dest, rev, force))
                raise DvcPullError("network down mid-run")
            return super().import_(repo_url, path, dest, cwd, rev, force, extra_args)

    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        # Both subscriptions of one repo, both already bumped to new pins.
        ApprovedProduct(repo="acme", registry_entry="e", pin="pinAnew", source_path="out-a"),
        ApprovedProduct(repo="acme", registry_entry="e", pin="pinBnew", source_path="out-b"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="out-a", contract_pin="pinAold",
                       artifact_pin="p", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(),
                       local_path=str(Path("downloads") / "acme" / "aaaaaaa-2026-01-01")),
        DownloadedItem(repo="acme", output="out-b", contract_pin="pinBold",
                       artifact_pin="p", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(),
                       local_path=str(Path("downloads") / "acme" / "bbbbbbb-2026-01-01")),
    ]).save(m_path)

    def factory(url, pin):
        raise AssertionError("source_path is set; factory must not be called")

    with pytest.raises(EnclavePullError):
        enclave_pull(_Client(), _SecondImportFails(), manifest_path=m_path,
                     force=True, producer_view_factory=factory)

    rows = {(d.output, d.contract_pin)
            for d in EnclaveManifest.load(m_path).downloaded}
    # Subscription 1 replaced its own row; subscription 2 failed, so BOTH its
    # old row (custody record of the data still on disk) must survive.
    assert ("out-b", "pinBold") in rows, (
        f"failing sibling's custody row was pruned by its sibling's success: {rows}"
    )
    assert ("out-a", "pinAnew") in rows


def test_missing_stat_resolves_relative_local_path_against_enclave_root(tmp_path):
    # Review round 1: production rows carry enclave-relative local_paths
    # (downloads_root derives from manifest_path.parent). The D7 stat must
    # anchor there, not on the process cwd — this test deliberately does NOT
    # chdir into the enclave.
    m_path = tmp_path / "enclave_manifest.yaml"
    rel = Path("downloads") / "acme" / "ppppppp-2026-01-01"
    (tmp_path / rel).mkdir(parents=True)
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="1", source_path="out"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="out", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(),
                       local_path=str(rel)),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        raise AssertionError("fast-skip must not resolve the producer")
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path,
                 producer_view_factory=factory, reporter=rep)
    assert rep.warnings == [], f"false missing report from foreign cwd: {rep.warnings}"


def test_force_pull_clears_rows_of_outputs_dropped_across_bump(tmp_path):
    # Review round 2: the (repo, output) force-prune only reaches a stale row
    # whose output name is still in the current pin's resolved set. A producer
    # that DROPPED or RENAMED an output across the bump leaves a row at the
    # old pin matching no current output — it must go too, or the D7 report
    # recurs on every pull and its own --force hint can never clear it.
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="pin2new", all=True),
    ], downloaded=[
        DownloadedItem(repo="acme", output="y", contract_pin="pin1old",
                       artifact_pin="p", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(),
                       local_path=str(tmp_path / "downloads" / "acme" / "aaa-old-y")),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        class View:
            def output_paths(self): return ["x"]  # "y" is gone at pin2new
        return View()
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path, force=True,
                 producer_view_factory=factory, reporter=rep)
    rows = [(d.output, d.contract_pin) for d in EnclaveManifest.load(m_path).downloaded]
    assert rows == [("x", "pin2new")], f"dropped-output row survived --force: {rows}"
    assert rep.warnings == []  # the hint's promise: --force clears the report


def test_repo_scoped_force_prune_leaves_other_repos_rows_alone(tmp_path):
    # The round-2 stale-pin prune must stay scoped to the repo whose imports
    # completed: a `--force acme` pull may not judge beta's rows against
    # acme's approved pins.
    m_path = tmp_path / "enclave_manifest.yaml"
    beta_dir = tmp_path / "downloads" / "beta" / "bbb-2026-01-01"
    beta_dir.mkdir(parents=True)
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="pin2new", all=True),
        ApprovedProduct(repo="beta", registry_entry="e", pin="pinBeta", source_path="out-b"),
    ], downloaded=[
        DownloadedItem(repo="acme", output="y", contract_pin="pin1old",
                       artifact_pin="p", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(),
                       local_path=str(tmp_path / "downloads" / "acme" / "aaa-old-y")),
        DownloadedItem(repo="beta", output="out-b", contract_pin="pinBeta",
                       artifact_pin="q", fetch_strategy="dvc-import",
                       downloaded_at=datetime.now(), local_path=str(beta_dir)),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        class View:
            def output_paths(self): return ["x"]
        return View()
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path, repo="acme",
                 force=True, producer_view_factory=factory, reporter=rep)
    rows = [(d.repo, d.output, d.contract_pin)
            for d in EnclaveManifest.load(m_path).downloaded]
    assert ("beta", "out-b", "pinBeta") in rows, f"bystander row pruned: {rows}"
    assert ("acme", "y", "pin1old") not in rows, f"stale row survived: {rows}"
    assert ("acme", "x", "pin2new") in rows
    assert rep.warnings == []


def test_pull_with_relative_subdir_manifest_reports_no_false_missing(tmp_path, monkeypatch):
    # Review round 2 (blast): a relative MULTI-SEGMENT manifest_path used to
    # store local_path with the parent prefix ("sub/downloads/...") which
    # missing_downloads re-prepended ("sub/sub/downloads/...") — so the very
    # row this pull just wrote was reported missing, with a --force hint.
    # Rows must be enclave-relative whenever manifest_path is.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    m_path = Path("sub") / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="acme", registry_entry="e", pin="1", source_path="out"),
    ]).save(m_path)
    rep = _WarnRecorder()
    def factory(url, pin):
        raise AssertionError("source_path is set; factory must not be called")
    enclave_pull(_Client(), _FakeDvcOps(), manifest_path=m_path,
                 producer_view_factory=factory, reporter=rep)
    assert rep.warnings == [], f"false missing report for a row just written: {rep.warnings}"
    d = EnclaveManifest.load(m_path).downloaded[0]
    # Posix separators regardless of host OS: the manifest crosses machines,
    # and `str(WindowsPath(...))` would record `downloads\acme\...`, which a
    # Linux reader sees as one filename and reports as missing.
    assert d.local_path.startswith("downloads/"), d.local_path
    assert "\\" not in d.local_path, d.local_path
    assert (Path("sub") / d.local_path).exists()
