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
from mintd.catalog import copied_leaves, rebuild_catalog
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
            files: dict[str, str | bytes] | None = None) -> str:
    """Write the product's `metadata.json` (or remove it, for None) and any
    other files, commit, and return the full sha."""
    if meta is None:
        (repo / "metadata.json").unlink(missing_ok=True)
    else:
        (repo / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for rel, body in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            (repo / rel).write_bytes(body)
        else:
            (repo / rel).write_text(body, encoding="utf-8")
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
    assert copied_leaves() == (
        "metadata.description", "metadata.tags", "ownership.team", "ownership.maintainers", "data_products.primary",
        "data_products.outputs[].path", "data_products.outputs[].description", "data_products.outputs[].primary",
        "status.state",
    )


def _owned_leaves(model: type[BaseModel], prefix: str = "") -> set[str]:
    """Every leaf path on ``model`` tagged USER or PIPELINE ("[]" for list items)."""
    leaves: set[str] = set()
    for name, info in model.model_fields.items():
        key = info.alias or name
        inner = _unwrap_container(info.annotation)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            listed = "list" in str(info.annotation)  # "[]" marks each item of a list of models, as copied_leaves does
            leaves |= _owned_leaves(inner, f"{prefix}{key}{'[]' if listed else ''}.")
        elif any(m in (Owner.USER, Owner.PIPELINE) for m in info.metadata):
            leaves.add(f"{prefix}{key}")
    return leaves


def test_the_copied_leaves_are_the_models_user_and_pipeline_leaves() -> None:
    """A rebuild copies exactly the leaves the model tags as the product's own.
    Mutation: copy only the USER leaves -> red."""
    assert set(copied_leaves()) == _owned_leaves(Metadata)


def _declare(where: str, bad: str) -> Callable[[dict[str, Any]], None]:
    if where == "primary":
        return lambda m: m["data_products"].update(primary=bad)
    return lambda m: m["data_products"]["outputs"].append(
        {"path": bad, "description": "x", "primary": False, "last_published": ""})


@pytest.mark.parametrize("where", ["output", "primary"])
@pytest.mark.parametrize(
    "bad", ["../x", "/abs/x", "data\\final", "", "-x", "data/fi\x00nal", "C:/x"],
    ids=["dotdot", "absolute", "backslash", "empty", "leading-dash", "nul", "drive"],
)
def test_an_output_path_a_consumer_cant_import_holds_the_entry(tmp_path: Path, where: str, bad: str) -> None:
    """An output's path is refused by the model, the primary by the same rule
    before check runs, so check is never handed it.

    Mutation: drop the primary's path rule -> check holds it instead, red."""
    repo, sha = _product(tmp_path, "demo", _meta("demo", _declare(where, bad)))
    path = _entry(tmp_path / "registry", "demo", repo)
    before = _read(path)

    [result] = _rebuild(tmp_path / "registry")

    entry = _read(path)
    assert result.verdict == "held", result.why
    refused_by = "data_products.primary must be" if where == "primary" else "metadata.json doesn't validate"
    assert result.why.startswith(refused_by), result.why
    assert entry["data_products"] == before["data_products"]
    assert entry["source"]["held"]["commit"] == sha
    assert "\n" not in result.why and (not bad or bad not in result.why.replace("'/' or '-'", ""))  # names no value


def test_a_product_mintd_check_refuses_holds_the_entry(tmp_path: Path) -> None:
    """A DVC project whose declared primary DVC doesn't track: check's error.

    Mutation: drop the check refusal -> the entry is updated."""
    repo, _ = _product(tmp_path, "demo", _meta("demo", lambda m: m["metadata"].update(description="new")),
                       files={".dvc/config": ""})
    path = _entry(tmp_path / "registry", "demo", repo)

    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "held"
    assert _read(path)["metadata"]["description"] != "new"
    assert result.why == ("mintd check reports 1 error(s) (data_products_output_untracked); "
                          "run mintd check in its repository")


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


_KEPT_WHY = {
    "held": "the repository has no metadata.json",
    "unreachable": "can't read the repository",
    "unreadable-entry": "the registry entry can't be read",
    "no-url": "the entry names no repository.github_url",
}


@pytest.mark.parametrize("case", list(_KEPT_WHY))
def test_an_entry_the_rebuild_doesnt_update_keeps_its_file_and_fields(tmp_path: Path, case: str) -> None:
    """Products are kept: a rebuild never deletes, empties or strips an entry.
    A held entry changes only `source.held`; an unreachable one, and one
    skipped (it can't be read, or names no repository), stays byte for byte.

    Mutations: unlink a skipped entry -> both skipped cases are red; write
    `{}` for a held one, or drop a field it doesn't own -> the held case is
    red."""
    registry, gone = tmp_path / "registry", tmp_path / "remotes" / "gone"
    if case == "held":
        repo, _ = _product(tmp_path, "demo", None)
        path = _entry(registry, "demo", repo, lambda e: e.update(source={"commit": "a" * 40}))
    elif case == "no-url":
        path = _entry(registry, "demo", gone, lambda e: e["repository"].update(github_url=""))
    else:
        path = _entry(registry, "demo", gone)
    if case == "unreadable-entry":
        path.write_text("{ not: [yaml\n", encoding="utf-8")
    before = path.read_bytes()

    [result] = _rebuild(registry)

    assert result.verdict == {"held": "held", "unreachable": "unreachable"}.get(case, "skipped")
    assert result.why.startswith(_KEPT_WHY[case]), result.why
    assert path.is_file() and path.read_bytes().strip()
    if case == "held":
        after, was = _read(path), yaml.safe_load(before)
        assert after["source"].pop("held")["why"] == result.why
        assert after == was
    else:
        assert path.read_bytes() == before


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


class _Recording(SubprocessRegistryGitOps):
    """Real git, recording each clone's keywords, and timing out on one URL."""

    def __init__(self, *, times_out: str | None = None) -> None:
        super().__init__()
        self.clones: list[dict[str, Any]] = []
        self.times_out = times_out

    def clone(self, url: str, dest: Path, **kw: Any) -> None:  # type: ignore[override]
        from mintd._subprocess import WallTimeoutExceeded

        self.clones.append(kw)
        if url == self.times_out:
            raise WallTimeoutExceeded(5)
        super().clone(url, dest, **kw)


def _good_and(tmp_path: Path, files: dict[str, str | bytes]) -> tuple[Path, Path]:
    """A registry with `demo`, its product carrying ``files``, beside a good `other`."""
    demo, _ = _product(tmp_path, "demo", _meta("demo", lambda m: m["metadata"].update(description="new")), files=files)
    other, _ = _product(tmp_path, "other", _meta("other", lambda m: m["metadata"].update(description="new")))
    _entry(tmp_path / "registry", "demo", demo)
    _entry(tmp_path / "registry", "other", other)
    return demo, other


@pytest.mark.parametrize("files", [
    {".dvc/config": b"\xff\xfe[core]\n"},
    {"enclave_manifest.yaml": b"\xff\xfe approved_products: []\n"},
    {"data/imports/x.dvc": b"\xff\xfe outs: []\n"},
    {"data/imports/x.dvc": "deps: 3\n"},
    {"enclave_manifest.yaml": "schema_version: '2.0'\nenclave_name: e\napproved_products: 3\n"},
], ids=["dvc-config-not-utf8", "manifest-not-utf8", "pointer-not-utf8", "pointer-deps-a-number", "manifest-invalid"])
def test_one_products_unreadable_file_holds_only_that_product(tmp_path: Path, files: dict[str, str | bytes]) -> None:
    """A file check can't read holds its own product, in one line naming no
    value from it; every other entry is still rebuilt.

    Mutation: drop the guard around check -> a traceback and no result."""
    _good_and(tmp_path, files)

    results = {r.name: r for r in _rebuild(tmp_path / "registry")}

    assert results["demo"].verdict == "held" and "\n" not in results["demo"].why, results["demo"].why
    assert results["other"].verdict == "updated"


def test_a_clone_that_times_out_is_unreachable_and_the_rest_go_on(tmp_path: Path) -> None:
    """Mutation: catch only GitOpError around the clone -> the timeout escapes."""
    demo, _ = _good_and(tmp_path, {})
    git = _Recording(times_out=str(demo))

    results = {r.name: r for r in rebuild_catalog(tmp_path / "registry", git_ops=git, write=True)}

    assert results["demo"].verdict == "unreachable" and results["demo"].why == (
        "can't read the repository (WallTimeoutExceeded)")
    assert results["other"].verdict == "updated"


def test_every_clone_skips_lfs_objects(tmp_path: Path) -> None:
    """Mutation: drop the env -> the clone smudges LFS pointers into downloads."""
    _good_and(tmp_path, {})
    git = _Recording()

    rebuild_catalog(tmp_path / "registry", git_ops=git, write=False)

    assert [c.get("env") for c in git.clones] == [{"GIT_LFS_SKIP_SMUDGE": "1"}] * 2


def test_clone_passes_its_env_to_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mutation: run git with `git_env()` alone -> the variable is missing."""
    import mintd._subprocess as sub

    seen: dict[str, str] = {}

    def fake(argv: list[str], **kw: Any) -> Any:
        seen.update(kw["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sub, "run_streaming", fake)
    SubprocessRegistryGitOps().clone(str(tmp_path / "src"), tmp_path / "dest", env={"GIT_LFS_SKIP_SMUDGE": "1"})

    assert seen.get("GIT_LFS_SKIP_SMUDGE") == "1"


def test_rebuild_exits_1_when_no_entry_could_be_read(tmp_path: Path) -> None:
    """Every repository unreachable (no access, say) is a failure, not a clean run.

    Mutation: count only updated and held -> exit 0."""
    _entry(tmp_path / "registry", "demo", tmp_path / "remotes" / "gone")
    registry = str(tmp_path / "registry")

    assert cli.main(["registry", "rebuild", "--registry", registry]) == 1
    assert cli.main(["registry", "rebuild", "--check", "--registry", registry]) == 1


def test_a_linked_metadata_json_holds_the_entry(tmp_path: Path) -> None:
    """A link could point out of the checkout: it isn't followed. Where git
    checks links out as plain files (Windows' default, `core.symlinks`
    false), the clone holds a file naming the target instead, which doesn't
    validate: held either way, and the linked file's fields never arrive.

    Mutation: drop the link refusal -> the linked file's fields arrive."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_meta("demo", lambda m: m["metadata"].update(description="from outside"))))
    repo, _ = _product(tmp_path, "demo", _meta("demo"))
    (repo / "metadata.json").unlink()
    try:
        (repo / "metadata.json").symlink_to(outside)
    except OSError:
        pytest.skip("no symlinks here")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "link")
    path = _entry(tmp_path / "registry", "demo", repo)

    [result] = _rebuild(tmp_path / "registry")

    assert result.verdict == "held"
    assert result.why in ("metadata.json is a link, which a rebuild doesn't follow",
                          "metadata.json doesn't validate (1 error(s)); run mintd check in its repository")
    assert _read(path)["metadata"]["description"] != "from outside"


def test_an_unreadable_registry_entry_is_named_to_its_subscribers(tmp_path: Path) -> None:
    """Mutation: drop the note -> the subscriber's reason doesn't say why its product is unknown."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    vault_repo = tmp_path / "remotes" / "data_vault"
    vault_repo.mkdir(parents=True)
    _git(vault_repo, "init", "-q", "-b", "main")
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="demo", registry_entry="catalog/data/demo.yaml", pin="a" * 40),
    ]).save(vault_repo / "enclave_manifest.yaml")
    _commit(vault_repo, _meta("vault"))
    _entry(tmp_path / "registry", "vault", vault_repo)
    (tmp_path / "registry" / "catalog" / "data" / "demo.yaml").write_text("{ not: [yaml\n")

    results = {r.name: r for r in _rebuild(tmp_path / "registry", write=False)}

    assert results["demo"].verdict == "skipped"
    assert results["vault"].verdict == "held"
    assert results["vault"].why.endswith("registry entries that can't be read: demo.yaml"), results["vault"].why


def test_mintds_own_failure_is_marked_and_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A type, key or attribute error out of check is more likely mintd's bug
    than the product's fault: one bad release would hold every product. The
    hold says so, and the run exits 1.

    Mutations: don't mark it -> `mintd_error` is false; leave it out of the
    exit code -> exit 0."""
    import mintd.check

    def broken(*_: Any, **__: Any) -> Any:
        raise TypeError("a bug")

    monkeypatch.setattr(mintd.check, "check_project", broken)
    _good_and(tmp_path, {})

    rc = cli.main(["--json", "registry", "rebuild", "--registry", str(tmp_path / "registry")])

    rows = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert [(r["verdict"], r["mintd_error"]) for r in rows] == [("held", True), ("held", True)]
    assert rows[0]["why"] == "mintd's error: check failed on this product (TypeError); tell mintd's maintainers"
