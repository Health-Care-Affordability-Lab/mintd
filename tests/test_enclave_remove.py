import pytest
from datetime import datetime
from mintd.enclave import AmbiguousSubscription, enclave_remove, ApprovedProduct, DownloadedItem, EnclaveManifest, ImportNotFound

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


# --- P5: one producer, many subscriptions (issue33) -------------------------
# D2 (user, 2026-08-21): bare `remove <repo>` was unambiguous only while a repo
# held one row. Single-row repos are unchanged; the tests above are all
# single-row and stay green.


def _two_row(tmp_path, extra_downloaded=()):
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
        ApprovedProduct(repo="b", registry_entry="e", pin="2"),
    ], downloaded=list(extra_downloaded)).save(m_path)
    return m_path


def test_remove_bare_refuses_when_repo_has_several_rows(tmp_path):
    m_path = _two_row(tmp_path)

    with pytest.raises(AmbiguousSubscription) as ei:
        enclave_remove(_Client(), manifest_path=m_path, name="a")

    assert ei.value.labels == ["data/x", "<primary>"]
    # A refusal destroys nothing.
    assert len(EnclaveManifest.load(m_path).approved_products) == 3


def test_remove_all_wipes_every_row_of_the_repo(tmp_path):
    m_path = _two_row(tmp_path)

    enclave_remove(_Client(), manifest_path=m_path, name="a", all_=True)

    assert [ap.repo for ap in EnclaveManifest.load(m_path).approved_products] == ["b"]


def test_remove_primary_leaves_the_path_subscription(tmp_path):
    """Without --primary there is no selector for the bare-primary row:
    --source-path can only name the row being kept, and --all wipes both."""
    m_path = _two_row(tmp_path)

    enclave_remove(_Client(), manifest_path=m_path, name="a", primary=True)

    m = EnclaveManifest.load(m_path)
    assert [(ap.repo, ap.source_path) for ap in m.approved_products] == [
        ("a", "data/x"), ("b", None),
    ]


def test_remove_primary_keeps_provenance_a_surviving_row_still_claims(tmp_path):
    """downloaded[] is the record of what is on disk. Dropping the row a
    surviving subscription still wants would make that data un-packageable."""
    m_path = _two_row(tmp_path, extra_downloaded=[
        DownloadedItem(repo="a", output="data/x", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
        DownloadedItem(repo="a", output="data/primary/", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp2"),
    ])

    enclave_remove(_Client(), manifest_path=m_path, name="a", primary=True)

    assert [d.output for d in EnclaveManifest.load(m_path).downloaded] == ["data/x"]


# --- review round 1 fixes ---------------------------------------------------


def test_remove_selector_that_matches_nothing_says_so_truthfully(tmp_path):
    """Repo-level absence and SELECTOR-level absence are different failures.
    Reporting the first for the second is a lie the user can check against
    `enclave list` -- and AmbiguousSubscription's hint routes them here, since
    on a [<path>, <all>] repo it offers `--primary` and no primary row exists."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True),
    ]).save(m_path)

    with pytest.raises(ImportNotFound) as ei:
        enclave_remove(_Client(), manifest_path=m_path, name="a", primary=True)

    msg = str(ei.value)
    assert "not in approved_products[]" not in msg, "the repo IS subscribed"
    assert "data/x" in msg and "<all>" in msg, "must name what it actually has"
    assert len(EnclaveManifest.load(m_path).approved_products) == 2


def test_remove_unknown_repo_still_says_not_in_approved_products(tmp_path):
    """Over-fire guard: the repo-absent contract is unchanged."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
    ]).save(m_path)

    with pytest.raises(ImportNotFound) as ei:
        enclave_remove(_Client(), manifest_path=m_path, name="ghost")
    assert "not in approved_products[]" in str(ei.value)


def test_remove_source_path_drops_provenance_even_if_a_primary_might_claim_it(tmp_path):
    """WHEN IN DOUBT, DROP. A surviving bare-primary row MIGHT resolve to the
    output being unsubscribed -- it is unknowable without a producer fetch --
    but the two errors are not symmetric: dropping a row a survivor wanted
    costs one re-fetch on the next pull; keeping a revoked row ships it into an
    enclave over a one-way transfer, because `enclave_package` selects purely
    off downloaded[] and never reads approved_products."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1"),
    ], downloaded=[
        DownloadedItem(repo="a", output="data/x", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
    ]).save(m_path)

    enclave_remove(_Client(), manifest_path=m_path, name="a", source_path="data/x")

    m = EnclaveManifest.load(m_path)
    assert [ap.source_path for ap in m.approved_products] == [None], "the path row is gone"
    assert [d.output for d in m.downloaded] == [], "and so is its provenance"


def test_remove_source_path_drops_provenance_when_no_survivor_can_claim_it(tmp_path):
    """Over-fire guard for the test above: with only sibling source_path rows
    left, nothing can resolve to the removed output, so the row goes."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/y"),
    ], downloaded=[
        DownloadedItem(repo="a", output="data/x", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
        DownloadedItem(repo="a", output="data/y", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp2"),
    ]).save(m_path)

    enclave_remove(_Client(), manifest_path=m_path, name="a", source_path="data/x")

    assert [d.output for d in EnclaveManifest.load(m_path).downloaded] == ["data/y"]


def test_remove_keeps_all_provenance_when_an_all_row_survives(tmp_path):
    """A surviving `all` row claims every output of the repo."""
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(repo="a", registry_entry="e", pin="1", source_path="data/x"),
        ApprovedProduct(repo="a", registry_entry="e", pin="1", all=True),
    ], downloaded=[
        DownloadedItem(repo="a", output="data/x", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp"),
        DownloadedItem(repo="a", output="data/y", contract_pin="1", artifact_pin="p",
                       fetch_strategy="dvc-import", downloaded_at=datetime.now(), local_path="lp2"),
    ]).save(m_path)

    enclave_remove(_Client(), manifest_path=m_path, name="a", source_path="data/x")

    assert [d.output for d in EnclaveManifest.load(m_path).downloaded] == ["data/x", "data/y"]
