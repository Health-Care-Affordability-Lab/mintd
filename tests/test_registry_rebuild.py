"""`mintd registry rebuild`: each catalog entry from its product's default branch.

Made-up products only. Each is a local git repository standing in for its
remote; the registry is a folder with `catalog/data/<name>.yaml` entries that
name those repositories.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from pydantic import BaseModel

from mintd import cli
from mintd._catalog_serializer import serialize
from mintd._registry_git_ops import SubprocessRegistryGitOps
from mintd.catalog import COPIED_LEAVES, rebuild_catalog
from mintd.model import Metadata, Owner, _unwrap_container

MINIMAL = Path(__file__).parent / "fixtures" / "metadata_v2_minimal.json"


def _git(repo: Path, *args: str, date: str = "2026-11-20T10:00:00+00:00") -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def _meta(name: str, mutate: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    meta = json.loads(MINIMAL.read_text(encoding="utf-8"))
    meta["project"]["name"] = name
    meta["project"]["full_name"] = f"data_{name}"
    meta["repository"]["github_url"] = f"https://github.com/example-org/data_{name}"
    if mutate is not None:
        mutate(meta)
    return meta


def _commit(repo: Path, meta: dict[str, Any] | None, *, date: str = "2026-11-20T10:00:00+00:00",
            files: dict[str, str] | None = None) -> str:
    """Write the product's `metadata.json` (or remove it, for None) and any
    other files, commit, and return the full sha."""
    if meta is None:
        (repo / "metadata.json").unlink(missing_ok=True)
    else:
        (repo / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "change", date=date)
    return _git(repo, "rev-parse", "HEAD").strip()


def _product(tmp_path: Path, name: str, meta: dict[str, Any] | None, **kw: Any) -> tuple[Path, str]:
    repo = tmp_path / "remotes" / f"data_{name}"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    return repo, _commit(repo, meta, **kw)


def _entry(registry: Path, name: str, repo: Path, mutate: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """The product's catalog entry as the registry holds it: built from the
    product's own metadata once, naming the local repository."""
    entry = yaml.safe_load(serialize(Metadata.model_validate(_meta(name))))
    entry["repository"]["github_url"] = str(repo)
    if mutate is not None:
        mutate(entry)
    path = registry / "catalog" / "data" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")
    return path


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rebuild(registry: Path, *, write: bool = True) -> list[Any]:
    return rebuild_catalog(registry, git_ops=SubprocessRegistryGitOps(), write=write)


def test_the_products_own_leaves_are_copied(tmp_path: Path) -> None:
    def changed(m: dict[str, Any]) -> None:
        m["metadata"]["description"] = "County income, new vintage"
        m["metadata"]["tags"] = ["income", "county"]
        m["ownership"]["team"] = "new-team"
        m["ownership"]["maintainers"] = ["someone"]
        m["data_products"]["outputs"] = [
            {"path": "data/final/", "description": "Final", "primary": True, "last_published": ""},
            {"path": "data/extra/", "description": "Extra", "primary": False, "last_published": ""},
        ]
        m["status"]["state"] = "deprecated"

    repo, sha = _product(tmp_path, "demo", _meta("demo", changed), date="2026-11-21T09:30:00+00:00")
    path = _entry(tmp_path / "registry", "demo", repo)

    [result] = _rebuild(tmp_path / "registry")

    entry = _read(path)
    assert result.verdict == "updated" and result.commit == sha
    assert entry["metadata"] == {"description": "County income, new vintage", "tags": ["income", "county"]}
    assert entry["ownership"] == {"team": "new-team", "maintainers": ["someone"]}
    assert [(o["path"], o["description"], o["primary"]) for o in entry["data_products"]["outputs"]] == [
        ("data/final/", "Final", True), ("data/extra/", "Extra", False)]
    assert entry["status"]["state"] == "deprecated"
    assert entry["status"]["last_updated"] == _git(repo, "log", "-1", "--format=%cI").strip()  # main's commit date
    assert entry["source"] == {"commit": sha}


#: A product whose own storage block matches its `.dvc/config`, and whose primary DVC tracks, so offline check
#: passes and the copy rule, not a hold, decides what reaches the entry.
_STORAGE = {"provider": "s3", "bucket": "demo-bucket", "prefix": "lab/data_demo/", "endpoint": "https://s3.example.com",
            "versioning": True, "dvc": {"remote_name": "data_demo"}}
_CONSISTENT_FILES = {
    ".dvc/config": "[core]\n    remote = data_demo\n['remote \"data_demo\"']\n"
                   "    url = s3://demo-bucket/lab/data_demo/\n    endpointurl = https://s3.example.com\n",
    "data/final.dvc": "outs:\n- md5: " + "a" * 32 + ".dir\n  size: 1\n  nfiles: 1\n  hash: md5\n  path: final\n",
}


def test_the_registrys_fields_and_unknown_keys_are_never_copied(tmp_path: Path) -> None:
    """The registry's workflows act on these: repository permissions, bucket
    policies and a public mirror. A product setting them in its own file
    changes nothing, while its own fields still arrive.

    Mutations: copy the product's file wholesale, or copy `access_control`,
    `repository.mirror.url`, `storage`, `governance` or `schema` -> the
    entry's field differs."""
    def hostile(m: dict[str, Any]) -> None:
        m["metadata"]["description"] = "arrives"
        m["access_control"]["teams"] = [{"name": "everyone", "permission": "admin"}]
        m["repository"]["mirror"] = {"url": "https://github.com/someone/public-copy", "purpose": "x"}
        m["storage"] = _STORAGE
        m["governance"]["classification"] = "public"
        m["project"]["name"] = "renamed"
        m["schema"] = {"location": "schemas/v9/schema.json"}
        m["surprise"] = {"nested": True}
        m["metadata"]["surprise"] = "nested key"

    def registrys(e: dict[str, Any]) -> None:
        e["governance"]["classification"] = "licensed"
        e["storage"] = {**_STORAGE, "bucket": "registry-bucket"}

    repo, _ = _product(tmp_path, "demo", _meta("demo", hostile), files=_CONSISTENT_FILES)
    path = _entry(tmp_path / "registry", "demo", repo, registrys)
    before = _read(path)

    [result] = _rebuild(tmp_path / "registry")

    entry = _read(path)
    assert result.verdict == "updated", result.why  # a hold would leave every field alone and prove nothing
    assert entry["metadata"]["description"] == "arrives"
    for field in ("access_control", "repository", "storage", "governance", "project", "schema"):
        assert entry.get(field) == before.get(field), field
    assert "surprise" not in entry and "surprise" not in entry["metadata"]
    assert set(result.reports) >= {
        "project.name differs from the entry's; the registry keeps its own (a registry PR changes it)",
        "storage differs from the entry's; the registry keeps its own (a registry PR changes it)",
        "governance.classification differs from the entry's; the registry keeps its own (a registry PR changes it)",
        "schema differs from the entry's; the registry keeps its own (a registry PR changes it)",
    }


def test_publish_stamps_never_reach_the_entry(tmp_path: Path) -> None:
    """Mutation: copy `outputs[].last_published` or `status.last_published_version`."""
    def stamped(m: dict[str, Any]) -> None:
        m["data_products"]["outputs"][0]["last_published"] = "2030-01-01"
        m["status"]["last_published_version"] = "9.9.9"

    def entry_stamps(e: dict[str, Any]) -> None:
        e["data_products"]["outputs"][0]["last_published"] = "2026-01-01"
        e["status"]["last_published_version"] = "1.0.0"

    repo, _ = _product(tmp_path, "demo", _meta("demo", stamped))
    path = _entry(tmp_path / "registry", "demo", repo, entry_stamps)

    _rebuild(tmp_path / "registry")

    entry = _read(path)
    assert entry["data_products"]["outputs"][0]["last_published"] == "2026-01-01"
    assert entry["status"]["last_published_version"] == "1.0.0"


def test_the_copied_leaves_are_pinned() -> None:
    """A new field the product may set is a reviewed choice: this changes with it."""
    assert COPIED_LEAVES == (
        "metadata.description", "metadata.tags", "ownership.team", "ownership.maintainers", "data_products.primary",
        "data_products.outputs[].path", "data_products.outputs[].description", "data_products.outputs[].primary",
        "status.state",
    )


def _owned_leaves(model: type[BaseModel], prefix: str = "") -> set[str]:
    """Every leaf path on ``model`` tagged USER or PIPELINE ("[]" for list items)."""
    leaves: set[str] = set()
    for name, info in model.model_fields.items():
        key = info.alias or name
        listed = "list" in str(info.annotation)
        path = f"{prefix}{key}{'[]' if listed else ''}"
        inner = _unwrap_container(info.annotation)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            leaves |= _owned_leaves(inner, f"{path}.")
        elif any(m in (Owner.USER, Owner.PIPELINE) for m in info.metadata):
            leaves.add(path)
    return leaves


@pytest.mark.xfail(strict=True, reason="until the model tags the registry's own fields Owner.REGISTRY")
def test_the_copied_leaves_agree_with_the_models_owner_tags() -> None:
    """When the model's re-marking lands this passes, the strict xfail fails,
    and the marker comes off: from then on the pinned list and the tags agree."""
    assert _owned_leaves(Metadata) == set(COPIED_LEAVES)


@pytest.mark.parametrize("bad", ["../x", "/abs/x", "data\\final", ""], ids=["dotdot", "absolute", "backslash", "empty"])
def test_an_output_path_a_consumer_cant_import_holds_the_entry(tmp_path: Path, bad: str) -> None:
    """Mutation: drop the output-path rule -> the entry takes the path."""
    repo, sha = _product(tmp_path, "demo", _meta("demo", lambda m: m["data_products"]["outputs"].append(
        {"path": bad, "description": "x", "primary": False, "last_published": ""})))
    path = _entry(tmp_path / "registry", "demo", repo)
    before = _read(path)

    [result] = _rebuild(tmp_path / "registry")

    entry = _read(path)
    assert result.verdict == "held"
    assert entry["data_products"] == before["data_products"]
    assert entry["source"]["held"]["commit"] == sha
    assert f"the output path {bad!r}" in entry["source"]["held"]["why"]


def test_a_product_mintd_check_refuses_holds_the_entry(tmp_path: Path) -> None:
    """A DVC project whose declared primary DVC doesn't track: check's error.

    Mutation: drop the check refusal -> the entry is updated."""
    repo, _ = _product(tmp_path, "demo", _meta("demo", lambda m: m["metadata"].update(description="new")),
                       files={".dvc/config": ""})
    path = _entry(tmp_path / "registry", "demo", repo)

    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "held"
    assert _read(path)["metadata"]["description"] != "new"
    assert "mintd check reports 1 error(s), first: 'data/final/' is not a DVC-tracked output" in result.why


@pytest.mark.parametrize("body", [None, "{not json"], ids=["missing", "invalid"])
def test_a_product_with_no_readable_metadata_holds_the_entry(tmp_path: Path, body: str | None) -> None:
    repo, _ = _product(tmp_path, "demo", _meta("demo"))
    if body is None:
        _commit(repo, None)
    else:
        _commit(repo, None, files={"metadata.json": body})
    _entry(tmp_path / "registry", "demo", repo)

    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "held"
    assert result.why.startswith("the repository has no metadata.json" if body is None else "metadata.json doesn't validate")


def test_the_next_good_commit_clears_the_hold(tmp_path: Path) -> None:
    """`source.held` keeps the first refusal's date across refusals, and the
    next good commit drops it.

    Mutation: never drop `source.held` -> it's still there."""
    def bad(m: dict[str, Any]) -> None:
        m["data_products"]["outputs"][0]["path"] = "../x"

    repo, first = _product(tmp_path, "demo", _meta("demo", bad), date="2026-11-20T10:00:00+00:00")
    path = _entry(tmp_path / "registry", "demo", repo)
    _rebuild(tmp_path / "registry")
    first_date = _git(repo, "log", "-1", "--format=%cI").strip()
    second = _commit(repo, _meta("demo", bad), date="2026-11-21T10:00:00+00:00")
    _rebuild(tmp_path / "registry")
    held = _read(path)["source"]["held"]
    assert (held["since"], held["commit"]) == (first_date, second)

    good = _commit(repo, _meta("demo"), date="2026-11-22T10:00:00+00:00")
    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "updated" and first != good
    assert _read(path)["source"] == {"commit": good}


def test_a_credential_in_the_products_url_is_reported_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: print the URL -> the token shows."""
    repo, _ = _product(tmp_path, "demo", _meta("demo", lambda m: m["repository"].update(
        github_url="https://x-access-token:s3cret@github.com/example-org/data_demo")))
    _entry(tmp_path / "registry", "demo", repo)

    [result] = _rebuild(tmp_path / "registry", write=False)
    rc = cli.main(["--json", "registry", "rebuild", "--check", "--registry", str(tmp_path / "registry")])

    assert ("metadata.json holds a credential in repository.github_url; remove it from the file and revoke it"
            in result.reports)
    assert rc == 1
    assert "s3cret" not in repr(result) and "s3cret" not in capsys.readouterr().out


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    """Mutation: write every entry whatever changed -> the file's bytes move."""
    repo, _ = _product(tmp_path, "demo", _meta("demo", lambda m: m["metadata"].update(description="new")))
    path = _entry(tmp_path / "registry", "demo", repo)
    _rebuild(tmp_path / "registry")
    after_first = path.read_bytes()

    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "unchanged" and path.read_bytes() == after_first


def test_only_products_with_an_entry_are_rebuilt(tmp_path: Path) -> None:
    """The entries are the join record: a repository with none gets none."""
    _product(tmp_path, "unlisted", _meta("unlisted"))
    repo, _ = _product(tmp_path, "demo", _meta("demo"))
    _entry(tmp_path / "registry", "demo", repo)

    results = _rebuild(tmp_path / "registry")

    assert [r.name for r in results] == ["demo"]
    assert sorted(p.name for p in (tmp_path / "registry" / "catalog").rglob("*.yaml")) == ["demo.yaml"]


def test_an_unreachable_repository_leaves_the_entry_untouched(tmp_path: Path) -> None:
    path = _entry(tmp_path / "registry", "demo", tmp_path / "remotes" / "gone")
    before = path.read_bytes()

    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "unreachable" and path.read_bytes() == before


def test_check_writes_nothing_and_says_whether_anything_would_change(tmp_path: Path) -> None:
    repo, _ = _product(tmp_path, "demo", _meta("demo", lambda m: m["metadata"].update(description="new")))
    path = _entry(tmp_path / "registry", "demo", repo)
    before = path.read_bytes()
    registry = str(tmp_path / "registry")

    assert cli.main(["registry", "rebuild", "--check", "--registry", registry]) == 1
    assert path.read_bytes() == before
    assert cli.main(["registry", "rebuild", "--registry", registry]) == 0
    assert cli.main(["registry", "rebuild", "--check", "--registry", registry]) == 0


def test_rebuild_outside_a_registry_checkout_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["registry", "rebuild", "--registry", str(tmp_path)]) == 1
    assert "has no catalog/ folder" in capsys.readouterr().err


def test_a_product_subscribing_to_a_catalogued_product_is_checked_against_the_registry(tmp_path: Path) -> None:
    """Check resolves an enclave manifest's products through the catalog: the
    registry checkout is that catalog, so a subscription to an entry there
    isn't an error.

    Mutation: give check no catalog -> "catalog client not provided", held."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    demo_repo, _ = _product(tmp_path, "demo", _meta("demo"))
    vault_repo = tmp_path / "remotes" / "data_vault"
    vault_repo.mkdir(parents=True)
    _git(vault_repo, "init", "-q", "-b", "main")
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="demo", registry_entry="catalog/data/demo.yaml", pin="a" * 40),
    ]).save(vault_repo / "enclave_manifest.yaml")
    _commit(vault_repo, _meta("vault", lambda m: m["metadata"].update(description="new")))
    _entry(tmp_path / "registry", "demo", demo_repo)
    _entry(tmp_path / "registry", "vault", vault_repo)

    results = {r.name: r for r in _rebuild(tmp_path / "registry")}

    assert results["vault"].verdict == "updated", results["vault"].why
