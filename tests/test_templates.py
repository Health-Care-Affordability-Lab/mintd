"""Tests for the Jinja-rendered scaffold engine (slice 19)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mintd._templates import (
    InitNameInvalid,
    render_scaffold,
    render_template,
    validate_project_name,
)
from mintd._producer_git_ops import GitArchiveFetcher
from mintd._templates.scaffolds import dispatch
from mintd.init import init_project
from mintd.model import Metadata
from mintd.publish import _resolve_version
from mintd.schema_ops import parse_published_schema

from tests._fakes.init_ops import _FakeInitOps
from tests._harness.dvc import real_dvc  # noqa: F401  (fixture)
from tests._harness.git import _git
from tests.scaffold_contract import is_ignored


# --- engine + name validation ---------------------------------------------

def test_render_template_substitutes_project_name() -> None:
    context = _MIN_CONTEXT | {"project_name": "alpha"}
    out = render_template("README_data.md.j2", context)
    assert "alpha" in out
    assert "{{" not in out
    assert "{%" not in out


def test_validate_project_name_rejects_leading_dash() -> None:
    with pytest.raises(InitNameInvalid):
        validate_project_name("-bad")


def test_validate_project_name_accepts_underscore_and_digits() -> None:
    validate_project_name("my_project_2")  # no exception


# --- per-type scaffolds ---------------------------------------------------

def test_data_python_renders_all_files(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "metadata.json" in rel
    assert "README.md" in rel
    assert "requirements.txt" in rel
    assert "code/fetch.py" in rel
    assert "code/ingest.py" in rel
    # Slice: the clean.* stub was deleted (demoted into ingest's parse_and_clean
    # helper); the scaffold must no longer emit it.
    assert "code/clean.py" not in rel
    assert "code/validate.py" in rel
    # Slice 41: scaffold no longer ships generate_schema.py; the CLI
    # (`mintd data schema generate`) replaces the vendored script.
    assert "schemas/generate_schema.py" not in rel
    assert "scripts/check-dvc-sync.sh" in rel


def test_project_python_renders_all_files(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="project", name="foo", language="python", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "metadata.json" in rel
    assert "citations.md" in rel
    assert "code/config.py" in rel
    assert "code/02_analysis/__init__.py" in rel


def test_code_renders_metadata_only(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="code", name="foo", language="python", target_dir=tmp_path
    )
    assert [p.relative_to(tmp_path).as_posix() for p in written] == ["metadata.json"]
    # Pin the Pydantic bypass: code-type metadata must validate AND carry
    # `project.type == "code"`. Regression coverage for a hardcoded "data".
    metadata = Metadata.model_validate_json((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata.project.type == "code"
    assert metadata.project.name == "foo"
    assert metadata.project.full_name == "foo"


@pytest.mark.parametrize(
    ("project_type", "expected_full_name"),
    [
        ("data", "data_foo"),
        ("project", "prj_foo"),
        ("enclave", "enclave_foo"),
        ("code", "foo"),
    ],
)
def test_full_name_prefix_convention(
    tmp_path: Path, project_type: str, expected_full_name: str
) -> None:
    """`code` carries the bare name; every other type keeps `<type>_<name>`
    (slice 39). `name` and `type` are unchanged across types."""
    render_scaffold(
        project_type=project_type, name="foo", language="python", target_dir=tmp_path
    )
    metadata = Metadata.model_validate_json(
        (tmp_path / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata.project.type == project_type
    assert metadata.project.name == "foo"
    assert metadata.project.full_name == expected_full_name


def test_enclave_renders_transfer_scripts(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "enclave_manifest.yaml" in rel
    assert "scripts/pull_data.sh" in rel
    assert "src/registry.py" in rel
    # The v1 inside-enclave path is gone: all four failed against a real v2
    # archive. Landing is `land.py`, shipped inside each transfer instead.
    assert "enclave_cli.py" not in rel
    assert "src/transfer.py" not in rel
    assert "scripts/unpack_transfer.sh" not in rel
    assert "scripts/verify_transfer.sh" not in rel
    # Orphaned by deleting src/transfer.py: its last line is
    # `python -m src.transfer package`.
    assert "scripts/package_transfer.sh" not in rel
    # requirements.txt told an air-gapped user to pip install.
    assert "requirements.txt" not in rel
    # enclave does NOT get the language-specific data files.
    assert "code/ingest.py" not in rel


# --- language matrix ------------------------------------------------------

def test_data_r_renders_r_sources(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="r", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "code/fetch.R" in rel
    assert "code/ingest.R" in rel
    assert "code/clean.R" not in rel
    assert "DESCRIPTION" in rel
    assert "renv.lock" in rel
    assert "code/ingest.py" not in rel


def test_data_stata_renders_do_sources(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="stata", target_dir=tmp_path
    )
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "code/fetch.do" in rel
    assert "code/ingest.do" in rel
    assert "code/clean.do" not in rel
    assert "stata-packages.txt" in rel
    assert "code/ingest.py" not in rel


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_data_dvc_yaml_pipeline_shape(tmp_path: Path, language: str) -> None:
    """Rendered dvc.yaml must match the immutable-raw governance model:
    ingest reads raw + writes intermediate; clean is not a stage; no active
    stage writes data/raw/; fetch ships only as a commented-out block."""
    import yaml

    render_scaffold(
        project_type="data", name="foo", language=language, target_dir=tmp_path
    )
    raw_text = (tmp_path / "dvc.yaml").read_text(encoding="utf-8")
    stages = yaml.safe_load(raw_text)["stages"]

    ingest = stages["ingest"]
    assert "../data/raw/" in ingest["deps"], "ingest must read data/raw/"
    assert ingest["outs"] == ["../data/intermediate/"], (
        "ingest must write data/intermediate/"
    )

    assert "clean" not in stages, "clean must not be a DVC stage (demoted to helper)"

    validate = stages["validate"]
    assert "../data/intermediate/" in validate["deps"]
    assert "../data/raw/" not in validate.get("deps", [])

    # Immutability invariant: no active stage declares data/raw/ as an out.
    for stage_name, stage in stages.items():
        assert "../data/raw/" not in stage.get("outs", []), (
            f"stage {stage_name!r} writes data/raw/ — immutability violation"
        )

    # fetch ships as a commented-out block, not an active stage.
    assert "fetch" not in stages, "fetch must be commented out, not an active stage"
    assert "#  fetch:" in raw_text, "commented fetch block must be present"

    # Schema generation: stata keeps a dedicated `schema` stage; py/r fold the
    # schema output into validate. Either way the DAG stays acyclic, and the
    # out is uncached so git keeps the file for the catalog, and persistent so
    # a failed run doesn't delete the committed file.
    schema_out = {"../schemas/v1/schema.json": {"cache": False, "persist": True}}
    if language == "stata":
        assert stages["schema"]["outs"] == [schema_out]
        assert schema_out not in validate.get("outs", [])
    else:
        assert "schema" not in stages
        assert schema_out in validate["outs"]


def test_python_scaffold_schema_types_and_order_match_the_cli_writer(tmp_path: Path) -> None:
    """The Python scaffold's generator names column types as the Frictionless
    writer does (`string`, `number`, `datetime`) on any pandas, and lists files
    by their relative path's characters, the same order on every OS.

    Mutations: test text columns only as `object` (pandas 3 reads them as
    `str`), drop the datetime branch, or sort the Path objects themselves ->
    red (the last on Windows).
    """
    import importlib.util

    render_scaffold(project_type="data", name="demo", language="python", target_dir=tmp_path)
    spec = importlib.util.spec_from_file_location("_scaffold_mintd_utils", tmp_path / "code" / "_mintd_utils.py")
    assert spec and spec.loader
    utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(utils)
    final = tmp_path / "final"
    final.mkdir()
    (final / "a.csv").write_text("n,x,label\n1,1.5,alpha\n2,2.5,beta\n", encoding="utf-8")
    (final / "B.csv").write_text("n\n1\n", encoding="utf-8")

    schema = utils.generate_data_schema(final / "a.csv")

    assert [(f["name"], f["type"]) for f in schema["schema"]["fields"]] == [
        ("n", "integer"), ("x", "number"), ("label", "string")
    ]
    assert [p.name for p in utils.find_data_files(final, [".csv"])] == ["B.csv", "a.csv"]
    # pandas reads a date-named JSON column as datetime64[...]
    (final / "events.json").write_text('[{"id": 1, "date": "2024-01-02"}]', encoding="utf-8")
    events = utils.generate_data_schema(final / "events.json")
    assert [(f["name"], f["type"]) for f in events["schema"]["fields"]] == [("id", "integer"), ("date", "datetime")]


def test_r_scaffold_schema_types_each_column_by_position(tmp_path: Path) -> None:
    """The R generator keeps the data's own column names, so they can repeat;
    each column is typed by its position, not looked up by a name another
    column shares.

    Mutation: look columns up by name (`df[[col_name]]`) -> red. Needs R; the
    text guard covers CI.
    """
    if not _r_ready():
        pytest.skip("Rscript with R6, readr and jsonlite is not installed")
    render_scaffold(project_type="data", name="demo", language="r", target_dir=tmp_path)
    data = tmp_path / "dup.csv"
    data.write_text("x,x\n1,hello\n2,world\n", encoding="utf-8")
    script = (
        f'source({json.dumps((tmp_path / "code" / "_mintd_utils.R").as_posix())}); '
        f's <- generate_data_schema({json.dumps(data.as_posix())}); '
        'cat(vapply(s$schema$fields, function(f) paste(f$name, f$type), ""), sep = "\\n")'
    )
    run = subprocess.run(["Rscript", "-e", script], capture_output=True, text=True, cwd=tmp_path / "code")
    assert run.returncode == 0, run.stderr
    assert run.stdout.split("\n")[:2] == ["x integer", "x string"]


@pytest.mark.parametrize("obj", ["table(c('06510', '06511', '06510'))", "matrix(1:4, ncol = 2)", "c(a = 1, b = 2)"])
def test_r_scaffold_schema_refuses_an_rds_that_is_not_a_table(tmp_path: Path, obj: str) -> None:
    """An .rds holding a table(), matrix or named vector is not a data frame:
    its element names are values (zip codes, IDs) and a matrix has one cell
    per "column". The generator refuses it, so validate warns and leaves the
    file out of the committed schema.

    Mutation: drop the data-frame check -> red. Needs R; the text guard
    covers CI.
    """
    if not _r_ready():
        pytest.skip("Rscript with R6, readr and jsonlite is not installed")
    render_scaffold(project_type="data", name="demo", language="r", target_dir=tmp_path)
    data = tmp_path / "obj.rds"
    script = (
        f'source({json.dumps((tmp_path / "code" / "_mintd_utils.R").as_posix())}); '
        f'saveRDS({obj}, {json.dumps(data.as_posix())}); '
        f'generate_data_schema({json.dumps(data.as_posix())})'
    )
    run = subprocess.run(["Rscript", "-e", script], capture_output=True, text=True, cwd=tmp_path / "code")
    assert run.returncode != 0
    assert "does not hold a data frame" in run.stderr


@pytest.mark.parametrize("project_type", ["data", "project", "code", "enclave"])
def test_metadata_names_a_schema_only_for_a_data_scaffold(tmp_path: Path, project_type: str) -> None:
    """A data scaffold's metadata.json names where its column schema is
    committed; other project types have no schema and get no key, not `null`.

    Mutations: drop the key, write it for every project type, or drop
    `exclude_if` -> red.
    """
    render_scaffold(project_type=project_type, name="demo", language="python", target_dir=tmp_path)  # type: ignore[arg-type]
    doc = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    if project_type == "data":
        assert doc["schema"] == {"location": "schemas/v1/schema.json"}
    else:
        assert "schema" not in doc


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_validate_stubs_no_stale_clean_reference(
    tmp_path: Path, language: str
) -> None:
    """Rendered validate scripts' no-data stop points users at `data/raw/` and
    `dvc repro`, not the demoted clean stage."""
    render_scaffold(
        project_type="data", name="foo", language=language, target_dir=tmp_path
    )
    ext = {"python": "py", "r": "R", "stata": "do"}[language]
    validate_script = (tmp_path / "code" / f"validate.{ext}").read_text(
        encoding="utf-8"
    )
    assert "Run clean." not in validate_script, (
        f"validate.{ext} must not reference the demoted clean stage"
    )
    assert _NO_DATA[language] in validate_script, (
        f"validate.{ext} no-data message must name the format and say to put files in data/raw/ and run dvc repro"
    )


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_data_scaffold_has_no_clean_stub_reference(
    tmp_path: Path, language: str
) -> None:
    """The `clean.*` stub was deleted; no rendered data-scaffold file may point
    back at it. Generalizes the per-file validate check to the whole tree, so a
    reintroduced pointer to the removed stub is caught anywhere it lands."""
    import re

    written = render_scaffold(
        project_type="data", name="foo", language=language, target_dir=tmp_path
    )
    pattern = re.compile(r"clean\.(py|R|do)")
    offenders = [
        p.relative_to(tmp_path).as_posix()
        for p in written
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{language}: rendered files still reference the deleted clean stub: "
        f"{offenders}"
    )


# --- the validate step ships the data, and the schema describes it --------

# Leading zeros make a rewrite of the copied file visible.
_SEED_CSV = b"zip,amount\n01234,1.5\n02345,\n"
_STATA = next(filter(None, map(shutil.which, ("stata-mp", "stata-se", "stata"))), None)


def _r_ready() -> bool:
    """Rscript plus the packages validate.R loads; a runner can have one without the other."""
    if shutil.which("Rscript") is None:
        return False
    probe = 'quit(status = as.integer(!all(vapply(c("R6","readr","jsonlite"), requireNamespace, logical(1), quietly = TRUE))))'
    return subprocess.run(["Rscript", "-e", probe], capture_output=True).returncode == 0


def _scaffold(tmp_path: Path, language: str) -> Path:
    """`mintd init data`'s own code path, without its git and dvc subprocesses."""
    return init_project(
        project_type="data", name="demo", target_dir=tmp_path, language=language, ops=_FakeInitOps()  # type: ignore[arg-type]
    )[0]


def _run_validate(root: Path, language: str) -> list[subprocess.CompletedProcess[str]]:
    """Run the scaffold's validate step from `code/` (its `wdir`); Stata also runs its `schema` stage."""
    code = root / "code"
    if language == "python":
        cmds = [[sys.executable, "validate.py"]]
    elif language == "r":
        if not _r_ready():
            pytest.skip("Rscript with R6, readr and jsonlite is not installed")
        cmds = [["Rscript", "validate.R"]]
    else:
        if _STATA is None:
            pytest.skip("Stata is not installed")
        cmds = [
            ["sh", "run_stata.sh", "validate", _STATA],
            [sys.executable, "-m", "mintd", "data", "schema", "generate", "--project-dir", ".."],
        ]
    return [subprocess.run(c, cwd=code, capture_output=True, text=True) for c in cmds]


def _committed_columns(raw: bytes) -> list[list[str]]:
    """The column names as the registry's reader sees them, after checking the
    committed file holds no values, counts, paths or timestamps."""
    doc = json.loads(raw)
    assert set(doc) <= {"generator", "schema_standard", "files"}, sorted(doc)
    for f in doc["files"]:
        assert set(f) <= {"filename", "path", "columns", "schema"}, sorted(f)
        assert set(f["schema"]) <= {"$schema", "fields", "missingValues"}, sorted(f["schema"])
        for c in f["schema"]["fields"]:
            assert set(c) <= {"name", "type", "title", "description", "categories"}, sorted(c)
    return [[c["name"] for c in t["fields"]] for t in parse_published_schema(raw)]


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_validate_step_schema_describes_the_checked_data(tmp_path: Path, language: str) -> None:
    """A fresh data scaffold's validate step ships the file it checked to
    `data/final/` unchanged, and the schema describes that file's columns,
    not the validation report. The report lands where git ignores it.

    The R and Stata cells need their runtimes and skip in CI; the template
    guard below covers them there.

    Mutations: report back into `FINAL_DIR`, delete the copy, copy through
    `df.to_csv` instead of the bytes, report into `logs/`, drop the report, or
    put back the `variables` shape, a row count, a statistic, a path or a
    timestamp in the schema -> red.
    """
    root = _scaffold(tmp_path, language)
    intermediate = root / "data" / "intermediate"
    intermediate.mkdir(parents=True, exist_ok=True)
    if language == "stata":
        import pandas as pd

        seed = intermediate / "claims.dta"
        pd.DataFrame({"zip": ["01234", "02345"], "amount": [1.5, None]}).to_stata(seed, write_index=False)
    else:
        seed = intermediate / "claims.csv"
        seed.write_bytes(_SEED_CSV)

    runs = _run_validate(root, language)

    for r in runs:
        assert r.returncode == 0, f"{r.args}: {r.stdout}\n{r.stderr}"
    final = root / "data" / "final"
    assert sorted(p.name for p in final.iterdir()) == [seed.name]
    assert (final / seed.name).read_bytes() == seed.read_bytes()
    assert _committed_columns((root / "schemas" / "v1" / "schema.json").read_bytes()) == [["zip", "amount"]]
    assert (root / "data" / f"validation_report.{'txt' if language == 'stata' else 'json'}").is_file()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", "data", "logs"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"the run left files git would commit: {status}"


def test_fresh_data_scaffold_commits_its_schema_where_the_catalog_entry_points(
    tmp_path: Path, real_dvc  # noqa: F811
) -> None:
    """After `dvc repro`, a fresh data scaffold's column schema is a file git
    keeps (not ignored, not in DVC's cache), its catalog entry names where it
    is, and the file at that path in the commit lists the data's columns. A
    forced rerun on unchanged data leaves it unchanged, and a run that stops
    for lack of data leaves it in place, so `git add -A` stages no deletion.

    Mutations: drop `cache: false` or `persist: true`, drop the scaffold's `schema` key or
    `serialize_by_alias`, exclude `schema` from the catalog, write the old
    `variables` shape, put back a row count or statistic, or put back the
    `generated_at` timestamp -> red.
    """
    root = _scaffold(tmp_path, "python")
    # The scaffold's `python` may lack pandas, and CI's Windows job does not
    # activate the venv: run the stages with this interpreter.
    dvc_vars = root / "dvc_vars.yaml"
    dvc_vars.write_text(
        dvc_vars.read_text(encoding="utf-8").replace("python_cmd: python", f"python_cmd: '\"{sys.executable}\"'"),
        encoding="utf-8",
    )
    _git(["init", "-q", "-b", "main"], cwd=root)
    real_dvc(["init", "-q"], cwd=root, check=True)
    (root / "data" / "raw" / "claims.csv").write_bytes(_SEED_CSV)

    run = real_dvc(["repro", "-q"], cwd=root)
    assert run.returncode == 0, run.stdout + run.stderr

    schema = "schemas/v1/schema.json"
    assert not is_ignored(root, schema)
    lock = yaml.safe_load((root / "dvc.lock").read_text(encoding="utf-8"))
    [md5] = [o["md5"] for o in lock["stages"]["validate"]["outs"] if o["path"] == f"../{schema}"]
    assert not (root / ".dvc" / "cache" / "files" / "md5" / md5[:2] / md5[2:]).exists()

    _git(["add", "-A"], cwd=root)
    _git(["commit", "-q", "--no-verify", "-m", "first"], cwd=root)
    sha = _git(["rev-parse", "HEAD"], cwd=root).strip()
    location = Metadata.from_json_file(root / "metadata.json").to_catalog_entry().model_dump()["schema"]["location"]
    assert location == schema
    raw = GitArchiveFetcher().fetch_path_at(str(root), sha, location)
    assert _committed_columns(raw) == [["zip", "amount"]]

    rerun = real_dvc(["repro", "-f", "-q"], cwd=root)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert _git(["status", "--porcelain", "--", "schemas"], cwd=root) == ""
    assert not is_ignored(root, schema)

    (root / "data" / "raw" / "claims.csv").unlink()
    no_data = real_dvc(["repro", "-q"], cwd=root)
    assert no_data.returncode != 0, no_data.stdout + no_data.stderr
    assert _git(["status", "--porcelain", "--", "schemas"], cwd=root) == ""

# The no-data stop names the format each validate step reads.
_NO_DATA = {
    "python": "no CSV files in data/intermediate/: put .csv source files in data/raw/ and run dvc repro",
    "r": "no CSV files in data/intermediate/: put .csv source files in data/raw/ and run dvc repro",
    "stata": "no .dta files in data/intermediate/: put .csv source files in data/raw/ and run dvc repro",
}


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_validate_step_without_data_stops_and_names_the_next_step(tmp_path: Path, language: str) -> None:
    """With nothing in `data/intermediate/`, validate stops non-zero, and the
    one line `dvc repro` shows says what to do next. It writes no schema and
    removes an earlier run's report (so nothing says PASSED for zero files),
    and leaves nothing git would commit.

    Mutations: remove the stop or the old report's removal in any language,
    or Stata's message relay in run_stata.sh, or the `logs/.msg_*` ignore
    line -> red.
    """
    root = _scaffold(tmp_path, language)
    (root / "data" / f"validation_report.{'txt' if language == 'stata' else 'json'}").write_text("Status: PASSED\n")

    validate = _run_validate(root, language)[0]

    assert validate.returncode != 0, f"{validate.stdout}\n{validate.stderr}"
    assert _NO_DATA[language] in validate.stderr, validate.stderr
    assert not (root / "schemas" / "v1" / "schema.json").exists()
    assert not list((root / "data").glob("validation_report.*"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", "data", "logs"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"the run left files git would commit: {status}"


# Without a header row, pandas and R take the first data row as column names.
_HEADERLESS = {
    "numbers": b"06510,12.5\n06511,3.0\n",
    "dates": b"2024-01-02,alpha\n2024-02-03,beta\n",
    "first-row-value": b"M,CT\nM,NY\n",
}


@pytest.mark.parametrize("csv", _HEADERLESS.values(), ids=_HEADERLESS.keys())
@pytest.mark.parametrize("language", ["python", "r"])
def test_validate_step_refuses_column_names_that_look_like_data(tmp_path: Path, language: str, csv: bytes) -> None:
    """A CSV without a header row would put its first row into the committed
    schema, so validate stops, says to add a header row, and writes none.

    Mutation: drop the check from either generator -> red. The R cells need
    R; the text guard covers CI.
    """
    root = _scaffold(tmp_path, language)
    intermediate = root / "data" / "intermediate"
    intermediate.mkdir(parents=True, exist_ok=True)
    (intermediate / "claims.csv").write_bytes(csv)

    validate = _run_validate(root, language)[0]

    assert validate.returncode != 0, validate.stdout + validate.stderr
    assert "add a header row" in validate.stderr, validate.stderr
    assert not (root / "schemas" / "v1" / "schema.json").exists()


@pytest.mark.parametrize("language", ["python", "r"])
def test_validate_step_reruns_over_a_read_only_input(tmp_path: Path, language: str) -> None:
    """After `dvc repro ingest` (or a pull of `data/intermediate/`), DVC can
    leave the intermediate files read-only. The copy into `data/final/`
    carries the bytes, not that mode, so a second run of validate by hand can
    overwrite its own copy. After a full `dvc repro`, `data/final/` is itself
    a read-only link, and reruns go through `dvc repro`, which removes outs
    first.

    Mutation: copy with the file's mode (`shutil.copy2`, or R's default
    `copy.mode = TRUE`) -> red.
    """
    root = _scaffold(tmp_path, language)
    intermediate = root / "data" / "intermediate"
    intermediate.mkdir(parents=True, exist_ok=True)
    seed = intermediate / "claims.csv"
    seed.write_bytes(_SEED_CSV)
    seed.chmod(0o444)

    for attempt in (1, 2):
        for r in _run_validate(root, language):
            assert r.returncode == 0, f"run {attempt}, {r.args}: {r.stdout}\n{r.stderr}"
    assert (root / "data" / "final" / seed.name).read_bytes() == _SEED_CSV


@pytest.mark.parametrize("language", ["r", "stata"])
def test_validate_template_ships_the_data_not_the_report(tmp_path: Path, language: str) -> None:
    """CI has neither R nor Stata, so the rendered scripts are checked as
    text: validate copies what it checked into `FINAL_DIR`, a failed R copy
    stops the step, and the report goes under `DATA_DIR`, never `FINAL_DIR`.
    For R also: the logger is built with `$new`, `log()` pastes its parts,
    and file extensions are matched with a regex, not a glob.

    Mutations: delete the copy, drop R's checked copy or its `copy.mode =
    FALSE`, put the report back in `FINAL_DIR`, drop `$new`, restore the
    one-argument `log()`, restore the `"*."` glob, put back a row count,
    path, timestamp or the `variables` list in the schema, drop the check on
    column names that look like data, or remove the empty-data stop, its
    removal of the old report, Stata's message file or the wrapper's relay of
    it, or the `logs/.msg_*` ignore line -> red.
    """
    render_scaffold(project_type="data", name="demo", language=language, target_dir=tmp_path)
    code = tmp_path / "code"
    ext, report = ("R", "validation_report.json") if language == "r" else ("do", "validation_report.txt")
    text = (code / f"validate.{ext}").read_text(encoding="utf-8")
    lines = text.splitlines()

    if language == "r":
        assert "if (!file.copy(data_file, file.path(FINAL_DIR, basename(data_file)), overwrite = TRUE, copy.mode = FALSE)) {" in text
        assert 'stop("Could not copy "' in text
    else:
        assert 'copy "$INTERMEDIATE_DIR/`file\'" "$FINAL_DIR/`file\'", replace' in text
    assert not [ln for ln in lines if "FINAL_DIR" in ln and "validation_report" in ln]
    assert [ln for ln in lines if report in ln], f"no line names {report}"
    assert all("DATA_DIR" in ln for ln in lines if report in ln)

    # With no data, validate stops with the one message that names the next step.
    assert _NO_DATA[language] in text
    if language == "r":
        assert "stop(no_data, call. = FALSE)" in text
        assert re.search(r"unlink\(report_file\)\n\s*stop\(no_data", text)
    else:
        assert 'capture erase "$DATA_DIR/validation_report.txt"' in text
        assert 'file open `msg_handle\' using "../logs/.msg_validate"' in text
        assert re.search(r"display as error \"`no_data'\"\n\s*exit 459\n", text)
        wrapper = (code / "run_stata.sh").read_text(encoding="utf-8")
        assert 'msg_file="../logs/.msg_${stage}"' in wrapper
        assert 'cat "${msg_file}" >&2' in wrapper
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        assert is_ignored(tmp_path, "logs/.msg_validate")

    if language == "r":
        stale = [p.name for p in code.glob("*.R") if re.search(r"\bParameterAwareLogger\(", p.read_text(encoding="utf-8"))]
        assert not stale, f"logger called without $new in: {stale}"
        utils = (code / "_mintd_utils.R").read_text(encoding="utf-8")
        assert 'log = function(..., level = "info")' in utils
        assert "paste0(...)" in utils
        assert 'pattern <- paste0("\\\\.", ext, "$")' in utils
        assert 'paste0("*.", ext)' not in utils
        # The committed schema records names and types only, under the data's
        # own column names, in a locale-independent file order.
        assert "fields <- c(fields, list(list(name = col_name, type = var_type)))" in utils
        assert "col_data <- df[[i]]" in utils  # by position: names can repeat or be blank
        assert 'stop(basename(data_path), " does not hold a data frame")' in utils
        assert "refuse_data_as_column_names(data_path, df)" in utils  # a headerless file stops validate
        assert "schema = list(fields = fields)" in utils
        assert "read.csv(data_path, stringsAsFactors = FALSE, check.names = FALSE)" in utils
        assert 'return(sort(files, method = "radix"))' in utils
        assert not re.search(r"\b(observations|missing_count|unique_values|filepath|generated_at|variables)\b", utils + text)


@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_readme_names_the_stage_that_writes_the_schema(tmp_path: Path, language: str) -> None:
    """The README names `schemas/v1/schema.json` once, on the line for the
    stage whose `outs` write it in `dvc.yaml`.

    Mutation: drop the schema path from the Python Validate line, or move
    Stata's mention off its Schema line -> red.
    """
    render_scaffold(project_type="data", name="demo", language=language, target_dir=tmp_path)
    stages = yaml.safe_load((tmp_path / "dvc.yaml").read_text(encoding="utf-8"))["stages"]
    writer = next(n for n, s in stages.items() if "schemas/v1/schema.json" in str(s.get("outs")))
    lines = [ln for ln in (tmp_path / "README.md").read_text(encoding="utf-8").splitlines() if "schemas/v1/schema.json" in ln]
    assert len(lines) == 1, lines
    assert f"**{writer.title()}**" in lines[0]


def test_run_all_do_fails_loud_on_missing_config(tmp_path: Path) -> None:
    """F5: run_all.do must guard `code/config.do` with `capture confirm file`
    + `exit 601`, so a missing config names the path instead of failing with a
    bare `r(601)`. A revert to a silent/unguarded load leaves this red."""
    render_scaffold(
        project_type="project", name="foo", language="stata", target_dir=tmp_path
    )
    run_all = (tmp_path / "run_all.do").read_text(encoding="utf-8")
    assert "capture confirm file" in run_all, (
        "run_all.do must probe for config with `capture confirm file`"
    )
    assert "exit 601" in run_all, (
        "run_all.do must `exit 601` on a missing config, not fall through"
    )
    assert 'do "`project_root\'/code/00_setup/config.do"' not in run_all, (
        "run_all.do must not reference the phantom code/00_setup/ dir"
    )


def test_run_all_r_fails_loud_on_missing_config(tmp_path: Path) -> None:
    """F5-R: run_all.R must `stop()` when `code/config.R` is missing rather than
    silently skipping the config load. A revert to the old
    `if (file.exists(config_path))` swallow leaves this red."""
    render_scaffold(
        project_type="project", name="foo", language="r", target_dir=tmp_path
    )
    run_all = (tmp_path / "run_all.R").read_text(encoding="utf-8")
    assert 'stop("Configuration not found' in run_all, (
        "run_all.R must stop() when config.R is missing"
    )
    assert '"00_setup"' not in run_all, (
        "run_all.R must not reference the phantom code/00_setup/ dir"
    )


# --- stata batch failure propagation ---------------------------------------
#
# Stata batch mode exits 0 even when a do-file aborts, so a bare
# `${stata_cmd} -b do ingest.do` stage lets DVC cache broken output and mark
# the stage clean. The scaffold routes stata stages through run_stata.sh,
# which reads the return code _mintd_run.do records in a sentinel file
# (logs/.rc_<stage>) and turns it into the process exit status.

_posix_sh_only = pytest.mark.skipif(
    os.name == "nt",
    reason="run_stata.sh is POSIX-only and rendered with CRLF on nt (Windows "
    "GA tracks .ps1 siblings — see project_windows_support_followup)",
)


def _render_stata_data(tmp_path: Path) -> Path:
    """Render a data/stata scaffold; return its code/ dir."""
    render_scaffold(
        project_type="data", name="foo", language="stata", target_dir=tmp_path
    )
    return tmp_path / "code"


def _write_fake_stata(tmp_path: Path, sentinel_value: str | None) -> Path:
    """A fake Stata binary: exits 0 unconditionally (the real batch-mode
    behavior this issue is about) and, unless ``sentinel_value`` is None,
    writes the sentinel the driver do-file would have written for the stage
    named by its last argument."""
    write = ""
    if sentinel_value is not None:
        write = (
            "mkdir -p ../logs\n"
            f"printf '%s' \"{sentinel_value}\" > \"../logs/.rc_$stage\"\n"
        )
    fake = tmp_path / "fake-stata"
    fake.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do stage="$a"; done\n'
        f"{write}"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_wrapper(code_dir: Path, fake: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "run_stata.sh", "ingest", str(fake)],
        cwd=code_dir,
        capture_output=True,
        text=True,
    )


def test_stata_dvc_stages_route_through_wrapper(tmp_path: Path) -> None:
    """Every active stata stage cmd must go through run_stata.sh; a bare
    `-b do` invocation swallows do-file aborts (Stata batch exits 0)."""
    import yaml

    code_dir = _render_stata_data(tmp_path)
    assert (code_dir / "run_stata.sh").is_file()
    driver = (code_dir / "_mintd_run.do").read_text(encoding="utf-8")
    # The driver must trap the stage script's abort (capture), record the
    # return code in the sentinel, and act as the batch entry point that
    # runs `main` (the stage stubs' interactive guard skips it in batch).
    assert "capture noisily do \"`stage'.do\"\nlocal rc = _rc" in driver, (
        "driver must capture the stage do-file AND record its return code"
    )
    assert "file write `fh' \"`rc'\"" in driver, (
        "driver must write the captured rc — not a constant — to the sentinel"
    )
    assert ".rc_" in driver
    # The main invocation must stay gated: only after the stage do-file
    # completed cleanly (rc == 0), and only if it defines a `main`
    # program (the `program list main` probe). Ungated, an aborted stage
    # do is followed by a clean `main` whose _rc overwrites rc with 0 —
    # sentinel says 0, wrapper exits 0, DVC caches the broken output:
    # the exact bug class this slice exists to fix.
    assert (
        "if `rc' == 0 {\n"
        "    capture program list main\n"
        "    if _rc == 0 {\n"
        "        capture noisily main\n"
        "        local rc = _rc\n"
        "    }\n"
        "}"
    ) in driver, "main must run gated on rc==0 and on main existing"

    raw_text = (tmp_path / "dvc.yaml").read_text(encoding="utf-8")
    assert "-b do" not in raw_text, (
        "dvc.yaml must not invoke Stata batch directly — exit 0 on abort"
    )
    stages = yaml.safe_load(raw_text)["stages"]
    assert stages["ingest"]["cmd"] == "sh run_stata.sh ingest ${stata_cmd}"
    assert stages["validate"]["cmd"] == "sh run_stata.sh validate ${stata_cmd}"
    # The wrapper and driver must be DEPS of both stages: without these
    # lines dvc never re-runs a stage when the wrapper or driver changes in
    # a scaffolded repo -- the stage looks clean while its harness rotted.
    # (Nothing else in the suite binds them; deleting all four lines left
    # every gate green until this loop existed.)
    for stage_name in ("ingest", "validate"):
        for dep in ("_mintd_run.do", "run_stata.sh"):
            assert dep in stages[stage_name]["deps"], (
                f"{dep} must be a dep of {stage_name}: dvc cannot see "
                "wrapper/driver changes otherwise"
            )
    # The optional fetch block must steer stata users through the wrapper
    # too: `${stata_cmd} -b do fetch.do` is an exit-0 no-op in batch (the
    # stub's guard skips main, so its `exit 198` tripwire never fires).
    assert "#    cmd: sh run_stata.sh fetch ${stata_cmd}" in raw_text, (
        "commented fetch example must route through run_stata.sh"
    )
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "logs/.rc_*" in gitignore, (
        "rc sentinels persist after success and must be git-ignored"
    )


@_posix_sh_only
def test_run_stata_wrapper_succeeds_on_rc_zero(tmp_path: Path) -> None:
    code_dir = _render_stata_data(tmp_path)
    fake = _write_fake_stata(tmp_path, "0")
    result = _run_wrapper(code_dir, fake)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "logs" / ".rc_ingest").read_text(encoding="utf-8") == "0"


@_posix_sh_only
@pytest.mark.parametrize("rc", ["601", "256"])
def test_run_stata_wrapper_fails_on_nonzero_rc(tmp_path: Path, rc: str) -> None:
    """Nonzero sentinel -> nonzero exit. rc=256 guards the mod-256 trap:
    `exit 256` would wrap around to status 0."""
    code_dir = _render_stata_data(tmp_path)
    fake = _write_fake_stata(tmp_path, rc)
    result = _run_wrapper(code_dir, fake)
    assert result.returncode != 0
    assert rc in result.stderr, result.stderr


@_posix_sh_only
def test_run_stata_wrapper_fails_on_corrupt_sentinel(tmp_path: Path) -> None:
    """A non-numeric sentinel means the driver was cut off mid-write (or
    the file was mangled); fail closed instead of guessing."""
    code_dir = _render_stata_data(tmp_path)
    fake = _write_fake_stata(tmp_path, "r(601);")
    result = _run_wrapper(code_dir, fake)
    assert result.returncode == 1
    assert "corrupt" in result.stderr, result.stderr


@_posix_sh_only
def test_run_stata_wrapper_fails_when_sentinel_missing(tmp_path: Path) -> None:
    """A Stata process that dies before the driver writes the sentinel must
    fail the stage, not pass it."""
    code_dir = _render_stata_data(tmp_path)
    fake = _write_fake_stata(tmp_path, None)
    result = _run_wrapper(code_dir, fake)
    assert result.returncode != 0
    # The missing-sentinel guard, not the corrupt-sentinel fallback: the
    # user must be told Stata died before recording, not "corrupt file".
    assert "without writing" in result.stderr, result.stderr
    assert ".rc_ingest" in result.stderr, result.stderr


@_posix_sh_only
def test_run_stata_wrapper_ignores_stale_sentinel(tmp_path: Path) -> None:
    """A leftover rc-0 sentinel from a previous run must not mask a Stata
    process that wrote nothing this run."""
    code_dir = _render_stata_data(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / ".rc_ingest").write_text("0", encoding="utf-8")
    fake = _write_fake_stata(tmp_path, None)
    result = _run_wrapper(code_dir, fake)
    # Exit 1 from the wrapper's own missing-sentinel check — not 127
    # ("run_stata.sh: not found") and not 0 (the stale sentinel believed).
    assert result.returncode == 1
    assert ".rc_ingest" in result.stderr, result.stderr


@_posix_sh_only
def test_run_stata_wrapper_usage_error_without_args(tmp_path: Path) -> None:
    """Missing <stata_cmd...> is a usage error: exit 2 with the usage
    line, not a set -u abort partway through the script."""
    code_dir = _render_stata_data(tmp_path)
    result = subprocess.run(
        ["sh", "run_stata.sh", "ingest"],
        cwd=code_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr, result.stderr


# --- correctness invariants -----------------------------------------------

def test_rendered_metadata_round_trips_through_pydantic(tmp_path: Path) -> None:
    render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    metadata = Metadata.model_validate_json((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata.project.name == "foo"
    assert metadata.project.full_name == "data_foo"
    assert metadata.project.type == "data"


def test_no_unrendered_jinja_markers_in_output(tmp_path: Path) -> None:
    written = render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text, f"unrendered marker in {path.name}: {text!r}"
        assert "{%" not in text, f"unrendered block in {path.name}: {text!r}"


@pytest.mark.parametrize("project_type", ["data", "project", "code", "enclave"])
@pytest.mark.parametrize("language", ["python", "r", "stata"])
def test_render_scaffold_matrix_no_unrendered_markers(
    tmp_path: Path, project_type: str, language: str
) -> None:
    """Render every (project_type, language) combination and assert that no
    template produces unrendered Jinja markers. Catches a `.j2` file with a
    typo or an undefined-variable reference in a rarely-exercised combo
    (e.g., project/r, project/stata) that the happy-path tests don't touch.
    """
    written = render_scaffold(
        project_type=project_type, name="foo", language=language, target_dir=tmp_path
    )
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text, f"{project_type}/{language}: unrendered marker in {path.name}"
        assert "{%" not in text, f"{project_type}/{language}: unrendered block in {path.name}"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX exec bit; the scaffolded .sh are bash-only (Windows GA "
    "tracks .ps1 siblings — see project_windows_support_followup)",
)
def test_enclave_shell_scripts_executable(tmp_path: Path) -> None:
    """Enclave transfer shell scripts must ship with 0o755. The legacy
    workflow `bash scripts/pull_data.sh` relies on the exec bit."""
    render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    for rel in ("scripts/pull_data.sh",):
        mode = (tmp_path / rel).stat().st_mode
        assert mode & 0o111, f"{rel} is not executable (mode {oct(mode)})"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX exec bit; the scaffolded .sh are bash-only (Windows GA "
    "tracks .ps1 siblings — see project_windows_support_followup)",
)
def test_data_scaffold_check_scripts_executable(tmp_path: Path) -> None:
    """The data scaffold's pre-commit-driven check scripts must also be exec."""
    render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )
    for rel in ("scripts/check-dvc-sync.sh", "scripts/check-env-lockfiles.sh"):
        mode = (tmp_path / rel).stat().st_mode
        assert mode & 0o111, f"{rel} is not executable (mode {oct(mode)})"


def test_scaffold_template_names_resolve_to_vendored_files() -> None:
    """Every template_name returned by every scaffold must exist on disk."""
    from importlib.resources import files
    available = {p.name for p in (files("mintd") / "files").iterdir()}
    for project_type in ("data", "project", "code", "enclave"):
        for language in ("python", "r", "stata"):
            _, file_list = dispatch(project_type)(language, "foo", f"{project_type}_foo")
            for _, template_name in file_list:
                assert template_name in available, (
                    f"{project_type}/{language}: missing template {template_name}"
                )


def test_gitignore_negates_dvc_pointers_under_downloads(tmp_path: Path) -> None:
    """The scaffolded `.gitignore` must keep bulk data under `downloads/`
    ignored while leaving DVC `.dvc` pointers trackable — else `dvc import`
    refuses to write into the enclave staging tree (slice 47, Failure 2)."""
    import subprocess

    from tests.scaffold_contract import is_ignored

    render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert not is_ignored(tmp_path, "downloads/provider-x/_staging/outputs.dvc")
    assert is_ignored(tmp_path, "downloads/provider-x/_staging/outputs/part.parquet")


def test_enclave_gitignore_covers_transfers(tmp_path: Path) -> None:
    """Transfer archives carry restricted data and are one `git add -A` from a
    commit. Landing receipts under `transfers/received/` must stay trackable —
    a receipt that can never be committed is not a receipt."""
    import subprocess

    from tests.scaffold_contract import is_ignored

    render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert is_ignored(tmp_path, "transfers/transfer-2026-05-15-000000.tar.gz")
    assert is_ignored(tmp_path, "incoming/land.py")
    assert not is_ignored(tmp_path, "transfers/received/transfer-2026-05-15-000000.yaml")


def test_enclave_readme_documents_landing(tmp_path: Path) -> None:
    """The scaffolded README is what points a researcher at a landing path. It
    used to send them to v1 scripts that failed against every v2 archive."""
    written = render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=tmp_path
    )
    readme = next(p for p in written if p.name == "README.md")
    text = readme.read_text(encoding="utf-8")

    assert "tar -xzf" in text
    assert "land.py" in text
    # The deleted v1 path, the wrong binary name, and a flag that never existed.
    assert "enclave_cli.py" not in text
    assert "unpack_transfer.sh" not in text
    assert "mint enclave" not in text
    assert "pull --all" not in text


@pytest.mark.parametrize("template_name", ["package.py.j2"])
def test_transfer_manifest_default_schema_version_validates(
    template_name: str, tmp_path: Path
) -> None:
    """A transfer manifest built from a source manifest that omits
    ``schema_version`` must default to ``"2.0"`` and validate against
    ``TransferManifest`` (slice 46). Guards the fallback default in the
    generated transfer/package scripts against future drift back to ``"1.0"``.
    """
    from mintd.enclave import TransferManifest

    source = render_template(template_name, _MIN_CONTEXT)
    namespace: dict[str, object] = {"__file__": str(tmp_path / "script.py")}
    exec(compile(source, template_name, "exec"), namespace)

    # Simulate an enclave manifest missing ``schema_version`` (hand-edited,
    # older file, or partial write) with no downloaded contents.
    namespace["load_manifest"] = lambda: {"enclave_name": "foo"}

    manifest = namespace["create_transfer_manifest"]({})

    assert manifest["schema_version"] == "2.0"
    TransferManifest.model_validate(manifest)


@pytest.mark.parametrize(
    "installed",
    [
        "0.0.2.dev110+g03b29b57b.d20260818",  # untagged build — what every install of main reports
        "0.1.0+d20260818",                    # tagged but dirty tree
        "0.1.0",                              # tagged, clean
    ],
)
def test_scaffold_stamps_a_publishable_mint_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    """`mint.version` must be plain semver whatever the install reports.

    setuptools-scm suffixes any untagged or dirty build, and `publish` validates
    this field *before* it looks at the requested version — so a suffixed string
    made every scaffolded repo unpublishable, with or without a flag.
    """
    monkeypatch.setattr(
        "mintd._templates._render.importlib.metadata.version", lambda _dist: installed
    )

    render_scaffold(
        project_type="data", name="foo", language="python", target_dir=tmp_path
    )

    stamped = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))["mint"]["version"]
    assert stamped == installed.split(".dev")[0].split("+")[0]
    # The real consumer, not a re-implementation of its regex.
    assert _resolve_version(stamped, None)


# --- helpers --------------------------------------------------------------

_MIN_CONTEXT: dict[str, object] = {
    "project_name": "foo",
    "full_project_name": "data_foo",
    "package_name": "foo",
    "project_type": "data",
    "language": "python",
    "language_version": "",
    "source_dir": "code",
    "created_at": "2026-01-01T00:00:00+00:00",
    "created_by": "tester",
    "author": "",
    "organization": "",
    "mint_version": "0.0.1",
    "mint_hash": "deadbeef",
    "platform_os": "linux",
    "command_sep": "&&",
    "stata_executable": "stata",
    "classification": "private",
    "contract_info": "",
    "description": "",
    "tags": [],
    "data_kind": "",
    "team": "",
    "admin_team": "",
    "researcher_team": "",
    "bucket_name": "",
    "storage_endpoint": "",
    "storage_prefix": "",
    "storage_provider": "s3",
    "storage_versioning": True,
    "dvc_remote_name": "origin",
    "registry_org": "",
    "registry_url": "",
    "mirror_url": "",
    "mirror_purpose": "",
    "data_products_primary": "",
}
