"""Tests for `DataDependency` + `scan_imports`."""

from __future__ import annotations

from pathlib import Path

import pytest

import yaml

from mintd.imports import DataDependency, NotAnImportError, scan_imports

FIXTURES = Path(__file__).parent / "fixtures" / "dvc_files"


def test_from_dvc_file_parses_standalone_import() -> None:
    dep = DataDependency.from_dvc_file(FIXTURES / "standalone_import.dvc")

    assert dep.kind == "dvc_file"
    assert dep.producer_repo == "https://github.com/example-org/provider-xw"
    assert dep.contract_pin == "4f7c2a1abcd1234567890abcdef0123456789abc"
    assert dep.output_path == "outputs/cms_based/"
    assert dep.local_path == "cms_based"
    assert dep.artifact_md5 == "e8f3a2b1c4d5e6f7a8b9c0d1e2f3a4b5"
    assert dep.stage_name is None


def test_from_dvc_file_skips_non_import() -> None:
    with pytest.raises(NotAnImportError):
        DataDependency.from_dvc_file(FIXTURES / "dvc_add_only.dvc")


def test_from_dvc_lock_yields_per_repo_dep() -> None:
    lock_path = FIXTURES / "dvc.lock"
    import yaml

    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))

    ingest_deps = DataDependency.from_dvc_lock_stage(
        "ingest_external", lock["stages"]["ingest_external"], lock_path
    )
    local_deps = DataDependency.from_dvc_lock_stage(
        "local_only", lock["stages"]["local_only"], lock_path
    )

    assert len(ingest_deps) == 1
    assert len(local_deps) == 0

    dep = ingest_deps[0]
    assert dep.kind == "dvc_lock_stage"
    assert dep.producer_repo == "https://github.com/example-org/provider-yy"
    assert dep.contract_pin == "aaaabbbbccccddddeeeeffff0011223344556677"
    assert dep.local_path == "data/imports/staging/"
    assert dep.output_path == ""
    assert dep.stage_name == "ingest_external"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_imports_walks_both_sources(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "imports" / "alpha.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )
    _write(tmp_path / "dvc.lock", (FIXTURES / "dvc.lock").read_text(encoding="utf-8"))

    deps = scan_imports(tmp_path)

    assert len(deps) == 2
    kinds = {d.kind for d in deps}
    assert kinds == {"dvc_file", "dvc_lock_stage"}


def test_scan_imports_dedup_dvc_file_wins(tmp_path: Path) -> None:
    # `.dvc` file: producer-xw, local_path "cms_based",
    # pin 4f7c2a1abcd1234567890abcdef0123456789abc.
    _write(
        tmp_path / "data" / "imports" / "cms.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )
    # dvc.lock stage referencing the same triple.
    _write(
        tmp_path / "dvc.lock",
        "schema: '2.0'\n"
        "stages:\n"
        "  ingest:\n"
        "    cmd: true\n"
        "    deps:\n"
        "      - path: cms_based\n"
        "        repo:\n"
        "          url: https://github.com/example-org/provider-xw\n"
        "          rev_lock: 4f7c2a1abcd1234567890abcdef0123456789abc\n",
    )

    deps = scan_imports(tmp_path)

    assert len(deps) == 1
    assert deps[0].kind == "dvc_file"


def test_scan_imports_handles_missing_dvc_lock(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "imports" / "alpha.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )

    deps = scan_imports(tmp_path)

    assert len(deps) == 1
    assert deps[0].kind == "dvc_file"


def test_scan_imports_handles_no_imports(tmp_path: Path) -> None:
    assert scan_imports(tmp_path) == []


def test_scan_imports_skips_dvc_add_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "imports" / "produced.dvc",
        (FIXTURES / "dvc_add_only.dvc").read_text(encoding="utf-8"),
    )
    _write(
        tmp_path / "data" / "imports" / "real.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )

    deps = scan_imports(tmp_path)

    assert len(deps) == 1
    assert deps[0].kind == "dvc_file"


def test_data_dependency_is_frozen() -> None:
    dep = DataDependency.from_dvc_file(FIXTURES / "standalone_import.dvc")
    with pytest.raises(Exception):
        dep.producer_repo = "mutated"  # type: ignore[misc]


def test_two_imports_sharing_a_basename_are_both_kept(tmp_path: Path) -> None:
    """The mirrored layout (D-A) lets two imports of DIFFERENT producer paths
    land under one namespace sharing a local basename — `data/final/` and
    `archive/final/` both write `outs[0].path: final`. Keying dedup on
    `local_path` alone collapsed them, so one became invisible to `check`
    and could never be bumped.

    Mutation: drop the differing-`output_path` arm of `_dedup` -> this test
    sees one dep instead of two.
    """
    def _import_dvc(producer_path: str) -> str:
        return (
            "outs:\n"
            "  - md5: e8f3a2b1c4d5e6f7a8b9c0d1e2f3a4b5\n"
            "    size: 1\n"
            "    path: final\n"
            "deps:\n"
            f"  - path: {producer_path}\n"
            "    repo:\n"
            "      url: https://github.com/example-org/provider-xw\n"
            "      rev: main\n"
            "      rev_lock: 4f7c2a1abcd1234567890abcdef0123456789abc\n"
        )

    ns = tmp_path / "data" / "imports" / "data_provider_xw"
    _write(ns / "data" / "final.dvc", _import_dvc("data/final/"))
    _write(ns / "archive" / "final.dvc", _import_dvc("archive/final/"))

    deps = scan_imports(tmp_path)

    assert len(deps) == 2
    assert sorted(d.output_path for d in deps) == ["archive/final/", "data/final/"]


@pytest.mark.parametrize(
    "repo_block",
    [
        {"url": "https://github.com/example-org/provider-xw", "rev_lock": None},
        {"url": "https://github.com/example-org/provider-xw"},
    ],
    ids=["null", "absent"],
)
def test_a_null_or_absent_rev_lock_reads_as_an_empty_pin(
    tmp_path: Path, repo_block: dict[str, str | None]
) -> None:
    """Both lanes, both spellings of "no pin at all".

    `rev_lock:` with nothing after it is what a hand-edit leaves behind, and
    deleting the key outright is the other half. Indexing `repo["rev_lock"]`
    turned each into a crash (pydantic `ValidationError` on the null, `KeyError`
    on the absent) out of `scan_imports`, which every caller -- `check`,
    `publish`, `registry register`, `data list` -- takes unhandled. They read as
    an empty pin instead, which `check` already refuses with `pin_missing`.

    Mutation: drop the `isinstance` guard in `_pin` -> this test raises.
    """
    _write(
        tmp_path / "data" / "imports" / "cms.dvc",
        yaml.safe_dump(
            {
                "outs": [{"path": "cms_based"}],
                "deps": [{"path": "outputs/cms_based/", "repo": repo_block}],
            }
        ),
    )
    _write(
        tmp_path / "dvc.lock",
        yaml.safe_dump(
            {
                "schema": "2.0",
                "stages": {
                    "ingest": {
                        "cmd": "true",
                        "deps": [{"path": "staging/", "repo": repo_block}],
                    }
                },
            }
        ),
    )

    deps = scan_imports(tmp_path)

    assert [(d.kind, d.contract_pin) for d in deps] == [("dvc_file", ""), ("dvc_lock_stage", "")]


@pytest.mark.parametrize(
    ("written", "yaml_reads_it_as"),
    [("4171780", 4171780), ("0123456", 42798)],
    ids=["decimal", "octal"],
)
def test_an_unquoted_numeric_rev_lock_is_refused_not_guessed(
    tmp_path: Path, written: str, yaml_reads_it_as: int
) -> None:
    """An unquoted all-digit pin reaches the parser as an `int`, and mintd
    refuses it in both lanes rather than guessing what the digits were.

    About one 7-hex short sha in 27 is all digits, so an unquoted
    `rev_lock: 4171780` is an ordinary hand-edit. Passing it straight to a
    `str` field raised a raw pydantic `ValidationError` out of `scan_imports`
    into every caller -- `check`, `publish`, `registry register`, `data list`.

    `str()`-ing it looks like the obvious repair and is a trap: YAML 1.1
    resolves a leading-zero, all-0-to-7 value as OCTAL, so a user who shortens
    a pin to `0123456` hands mintd 42798 and the digits they typed are gone
    before this code runs. `str()` would mint `"42798"` -- a different pin,
    the right shape, no warning -- and `check` would then compare the import
    against the wrong revision and call it up to date. The two ids below are
    the same hand-edit; only one of them survives a round trip, and nothing at
    this layer can tell them apart.

    So both read as the empty pin `check` already refuses with `pin_missing`.
    The cost is that a hand-edited all-digit pin needs quotes; the gain is that
    a wrong pin can never be silent.

    Mutation: `contract_pin=str(value)` in `_pin` -> the octal case reads
    "42798" and this test fails.
    """
    # Raw YAML, unquoted, because the bytes on disk are the input: a
    # `safe_dump` of the equivalent dict would quote the value and hide
    # whether it ever reaches the parser as an int at all.
    assert yaml.safe_load(f"rev_lock: {written}")["rev_lock"] == yaml_reads_it_as

    _write(
        tmp_path / "data" / "imports" / "cms.dvc",
        "outs:\n"
        "- path: cms_based\n"
        "deps:\n"
        "- path: outputs/cms_based/\n"
        "  repo:\n"
        "    url: https://github.com/example-org/provider-xw\n"
        f"    rev_lock: {written}\n",
    )
    _write(
        tmp_path / "dvc.lock",
        "schema: '2.0'\n"
        "stages:\n"
        "  ingest:\n"
        "    cmd: 'true'\n"
        "    deps:\n"
        "    - path: staging/\n"
        "      repo:\n"
        "        url: https://github.com/example-org/provider-xw\n"
        f"        rev_lock: {written}\n",
    )

    assert [(d.kind, d.contract_pin) for d in scan_imports(tmp_path)] == [
        ("dvc_file", ""),
        ("dvc_lock_stage", ""),
    ]
