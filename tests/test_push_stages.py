"""`mintd data push` with a pipeline whose stages haven't all run, real dvc
against an S3 server.

A stage dvc lists (`dvc stage list --name-only`, foreach and matrix
instances expanded by dvc's own templating and YAML) that has no hashed lock
entry, or records fewer outs than it declares, makes DVC push nothing for the
missing outs, at exit 0. A push that includes such a stage refuses before any
upload, on every remote type, with or without a dvc.lock; one naming only
other stages or `.dvc` files goes ahead. The refusal's hint spells out two
commands, and the tests run them as a shell would split them, so an example
that doesn't work fails here.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from mintd import cli
from tests._harness.dvc import real_dvc  # noqa: F401  (fixture)
from tests._harness.git import _git
from tests._harness.s3 import BUCKET


def _cmd(path: str) -> str:
    """A stage command writing ``path``, as a YAML string."""
    code = f"import pathlib; p=pathlib.Path('{path}'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('{path}')"
    return json.dumps(f'"{sys.executable}" -c "{code}"')


_DATA_FINAL_A_CSV = _cmd("data/final/a.csv")
_DATA_OTHER_CSV = _cmd("data/other.csv")
_DATA_FAN_I_CSV = _cmd("data/fan_${item}.csv")
_DATA_MX_K_CSV = _cmd("data/mx_${item.k}.csv")
_DATA_T_I_CSV = _cmd("data/t_${item}.csv")
_DATA_I_I_CSV = _cmd("data/i_${item}.csv")
_DATA_YN_I_CSV = _cmd("data/yn_${item}.csv")
_DATA_ON_CSV, _DATA_YES_CSV, _DATA_NO_CSV, _DATA_OFF_CSV = (_cmd(f"data/{s}.csv") for s in ("on", "yes", "no", "off"))

# Literal, templated (${lst}) and interpolated ('${v}') foreach stages, a
# matrix, YAML 1.2 values (yes, no, 012, 1e3), and stages named on, yes, no
# and off, which YAML 1.1 reads as booleans: named as dvc names them.
DVC_YAML = f"""\
vars:
  - lst: [m, n]
  - v: zz
stages:
  build:
    cmd: {_DATA_FINAL_A_CSV}
    outs:
    - data/final/a.csv
  other:
    cmd: {_DATA_OTHER_CSV}
    outs:
    - data/other.csv
  fan:
    foreach: [p q, r s]
    do:
      cmd: {_DATA_FAN_I_CSV}
      outs:
      - data/fan_${{item}}.csv
  mx:
    matrix:
      k: [x, y]
    cmd: {_DATA_MX_K_CSV}
    outs:
    - data/mx_${{item.k}}.csv
  tmpl:
    foreach: ${{lst}}
    do:
      cmd: {_DATA_T_I_CSV}
      outs:
      - data/t_${{item}}.csv
  interp:
    foreach: ['${{v}}', b]
    do:
      cmd: {_DATA_I_I_CSV}
      outs:
      - data/i_${{item}}.csv
  yn:
    foreach: [yes, no, 012, 1e3]
    do:
      cmd: {_DATA_YN_I_CSV}
      outs:
      - data/yn_${{item}}.csv
  on:
    cmd: {_DATA_ON_CSV}
    outs:
    - data/on.csv
  yes:
    cmd: {_DATA_YES_CSV}
    outs:
    - data/yes.csv
  no:
    cmd: {_DATA_NO_CSV}
    outs:
    - data/no.csv
  off:
    cmd: {_DATA_OFF_CSV}
    outs:
    - data/off.csv
"""
RAN = ["build", "fan@p q", "mx@x", "tmpl@m", "interp@zz", "interp@b", "yn@yes", "yn@no", "yn@12", "on", "no"]
NEVER_RAN = ["dvc.yaml:other", "dvc.yaml:fan@r s", "dvc.yaml:mx@y", "dvc.yaml:tmpl@n", "dvc.yaml:yn@1000.0",
             "dvc.yaml:yes", "dvc.yaml:off", "sub/dvc.yaml:sother"]


def _pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_dvc, s3_server, *,  # noqa: F811
              version_aware: bool, lock: str = "partial") -> Path:
    """A git repo with a dvc project at the S3 server: `data/raw.dvc` and
    `data/my file.csv.dvc`; the stages of ``DVC_YAML``; a nested pipeline
    `sub/` with `sbuild` and `sother`. ``lock``: "partial" runs ``RAN`` and
    sbuild only; "none" runs nothing, so there is no dvc.lock; "empty" then
    leaves the root lock `stages: {}`; "orphan" also records a stage `gone`
    dvc.yaml no longer declares; "broken" leaves sub/dvc.yaml unreadable.
    Committed and pushed to a bare upstream; no data pushed yet."""
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
        real_dvc.env[k] = v
    for k in ("HOME", "USERPROFILE", "DVC_GLOBAL_CONFIG_DIR", "DVC_SYSTEM_CONFIG_DIR", "DVC_SITE_CACHE_DIR"):
        monkeypatch.setenv(k, real_dvc.env[k])
    config = tmp_path / "mintd-config"
    config.mkdir()
    (config / "config.yaml").write_text(f"cache_dir: {tmp_path / 'mintd-cache'}\n")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(config))
    monkeypatch.setenv("COLUMNS", "1000")  # hints are read whole; a wrap can split an argument

    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["config", "user.email", "t@t"], cwd=proj)
    _git(["config", "user.name", "T"], cwd=proj)
    real_dvc(["init", "-q"], cwd=proj, check=True)
    real_dvc(["remote", "add", "-d", "-f", "storage", f"s3://{BUCKET}/lab/a"], cwd=proj, check=True)
    real_dvc(["remote", "modify", "storage", "endpointurl", s3_server.endpoint], cwd=proj, check=True)
    if version_aware:
        real_dvc(["remote", "modify", "storage", "version_aware", "true"], cwd=proj, check=True)
    (proj / "data" / "raw").mkdir(parents=True)
    (proj / "data" / "raw" / "x.csv").write_text("x\n")
    (proj / "data" / "my file.csv").write_text("m\n")
    real_dvc(["add", "-q", "data/raw", "data/my file.csv"], cwd=proj, check=True)
    (proj / "dvc.yaml").write_text(DVC_YAML)
    (proj / "sub").mkdir()
    (proj / "sub" / "dvc.yaml").write_text(yaml.safe_dump({"stages": {
        "sbuild": {"cmd": json.loads(_cmd("out.csv")), "outs": ["out.csv"]},
        "sother": {"cmd": json.loads(_cmd("sother.csv")), "outs": ["sother.csv"]},
    }}, sort_keys=False))
    if lock != "none":
        r = real_dvc(["repro", "-q", *RAN, "sub/dvc.yaml:sbuild"], cwd=proj)
        assert r.returncode == 0, r.stderr
    if lock == "empty":
        (proj / "dvc.lock").write_text("schema: '2.0'\nstages: {}\n")
    elif lock == "orphan":  # inserted as text: a YAML 1.1 round trip would rewrite the on/no keys as booleans
        text = (proj / "dvc.lock").read_text()
        build = yaml.safe_load(text)["stages"]["build"]
        gone = dict(build, outs=[dict(build["outs"][0], path="data/gone.csv")])
        entry = "".join(f"  {line}\n" for line in yaml.safe_dump({"gone": gone}, sort_keys=False).splitlines())
        (proj / "dvc.lock").write_text(text.replace("stages:\n", "stages:\n" + entry, 1))
        (proj / "data" / "gone.csv").write_text("data/final/a.csv")
    elif lock == "broken":
        (proj / "sub" / "dvc.yaml").write_text("stages:\n  sbuild:\n    cmd: [\n")
    elif lock == "int-key":  # YAML 1.2 reads 012 as the number 12, which dvc rejects as a stage name
        with (proj / "sub" / "dvc.yaml").open("a") as f:
            f.write("  012:\n    cmd: run\n    outs:\n    - z.csv\n")
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "track"], cwd=proj)
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)])
    _git(["remote", "add", "origin", str(origin)], cwd=proj)
    _git(["push", "-q", "-u", "origin", "main"], cwd=proj)
    monkeypatch.chdir(proj)
    return proj


def _err(capsys) -> str:
    return " ".join(capsys.readouterr().err.split())


def _command(err: str, after: str, before: str) -> list[str]:
    """The command a hint spells out between ``after`` and ``before``, split
    as a shell would split it."""
    start = err.index(after) + len(after)
    return shlex.split(err[start:err.index(before, start)])


@pytest.mark.parametrize("version_aware", [True, False], ids=["version-aware", "md5-layout"])
def test_a_full_push_beside_stages_never_run_refuses_and_both_hint_commands_work(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys, version_aware: bool,  # noqa: F811
) -> None:
    """`dvc repro` of some stages leaves the others with no lock entry,
    instances included, named as dvc names them: other, fan@r s, mx@y,
    tmpl@n (a templated foreach), yn@1000.0 (`1e3` read as YAML 1.2), the
    stages yes and off (YAML 1.1 reads both as booleans) and sub's sother. A full push refuses before any upload, naming exactly
    those, on either remote type; `push dvc.yaml:other` refuses too. The
    hint's push-the-rest command pushes, and after its `dvc repro` command
    a full push goes through, so no stage that ran (interp@zz, yn@yes,
    yn@12, on, no, ...) is taken for one that didn't.

    Mutations: drop the arm for a stage with no lock entry -> exit 0, red;
    name stages from dvc.yaml's keys instead of dvc -> the instances are
    missed or misnamed, red; drop the quoting -> the commands split wrong,
    red.
    """
    _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=version_aware)
    puts = len(s3_server.puts)

    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert f"dvc.lock records fewer outputs than dvc.yaml declares for {', '.join(NEVER_RAN)}, so" in err, err
    assert len(s3_server.puts) == puts
    assert cli.main(["data", "push", "dvc.yaml:other"]) == 1
    assert "for dvc.yaml:other, so" in _err(capsys)

    rest = _command(err, "for example ", "; if dvc.lock")
    assert rest == ["mintd", "data", "push", "dvc.yaml:build", "dvc.yaml:fan@p q", "dvc.yaml:mx@x"]
    assert cli.main(rest[1:]) == 0, _err(capsys)
    assert len(s3_server.puts) > puts

    repro = _command(err, "hint: run ", ", then push")
    assert repro == ["dvc", "repro", *NEVER_RAN]
    r = real_dvc(repro[1:], cwd=Path.cwd())
    assert r.returncode == 0, r.stderr
    assert cli.main(["data", "push"]) == 0, _err(capsys)


@pytest.mark.parametrize("targets, refused, named", [
    (["dvc.yaml:tmpl@n"], True, "dvc.yaml:tmpl@n"),
    (["tmpl@n"], True, "dvc.yaml:tmpl@n"),
    (["dvc.yaml:tmpl"], True, "dvc.yaml:tmpl@n"),
    (["dvc.yaml:tmpl@m"], False, "dvc.yaml:tmpl@n"),
    (["dvc.yaml:off"], True, "dvc.yaml:off"),
    (["off"], True, "dvc.yaml:off"),
    (["dvc.yaml:on"], False, "dvc.yaml:on"),
], ids=["instance-key", "bare-instance", "base", "the-instance-that-ran", "off-key", "bare-off", "on-that-ran"])
def test_a_stage_that_never_ran_stops_a_push_naming_it(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys, targets: list[str], refused: bool, named: str,  # noqa: F811
) -> None:
    """`foreach: ${lst}` expands only in dvc's templating: tmpl@m ran,
    tmpl@n didn't. A push naming tmpl@n, or the stage tmpl, refuses before
    any upload; one naming tmpl@m goes ahead. A stage named `off` never ran
    and `on` did: YAML 1.1 reads both keys as booleans, dvc as names.

    Mutations: name stages from dvc.yaml's keys -> tmpl@n is never seen,
    exit 0, red; read dvc.yaml or dvc.lock as YAML 1.1 -> off passes, or on
    is refused, red.
    """
    _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=False)
    puts = len(s3_server.puts)

    assert cli.main(["data", "push", *targets]) == (1 if refused else 0)
    err = _err(capsys)
    assert (f"for {named}, so" in err) is refused, err
    if refused:
        assert len(s3_server.puts) == puts


@pytest.mark.parametrize("version_aware", [True, False], ids=["version-aware", "md5-layout"])
def test_a_pipeline_never_run_has_no_lock_and_stops_a_push_that_includes_it(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys, version_aware: bool,  # noqa: F811
) -> None:
    """Nothing ran, so there is no dvc.lock at all. A full push, or one
    naming a stage of either pipeline, refuses before any upload; the
    full push's rest, the `.dvc` files, quoted, pushes.

    Mutations: skip a pipeline with no dvc.lock -> the .dvc data pushes at
    exit 0 and the stages say "already up to date", red.
    """
    _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=version_aware, lock="none")
    puts = len(s3_server.puts)

    for targets in ([], ["dvc.yaml:build"], ["sub/dvc.yaml:sbuild"]):
        assert cli.main(["data", "push", *targets]) == 1, targets
        err = _err(capsys)
        assert "fewer outputs than dvc.yaml declares for" in err and "dvc repro" in err, err
        assert len(s3_server.puts) == puts
    assert cli.main(["data", "push"]) == 1
    rest = _command(_err(capsys), "for example ", "; if dvc.lock")
    assert rest == ["mintd", "data", "push", "data/my file.csv.dvc", "data/raw.dvc"]
    assert cli.main(rest[1:]) == 0, _err(capsys)


@pytest.mark.parametrize("target", ["dvc.yaml:fan@p q", "fan@p q"])
def test_an_empty_lock_refuses_a_push_naming_one_foreach_instance(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys, target: str,  # noqa: F811
) -> None:
    """With the root lock `stages: {}`, naming one instance of a foreach
    stage includes a stage that never ran: refused before any upload, not
    "already up to date". With every root stage behind, the full push's
    push-the-rest command is sub's stage and the `.dvc` files, quoted.

    Mutation: compare the named instance with its base only -> exit 0, red.
    """
    _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=False, lock="empty")
    puts = len(s3_server.puts)

    assert cli.main(["data", "push", target]) == 1
    assert "for dvc.yaml:fan@p q, so" in _err(capsys)
    assert len(s3_server.puts) == puts

    assert cli.main(["data", "push"]) == 1
    rest = _command(_err(capsys), "for example ", "; if dvc.lock")
    assert rest == ["mintd", "data", "push", "sub/dvc.yaml:sbuild", "data/my file.csv.dvc", "data/raw.dvc"]
    assert cli.main(rest[1:]) == 0, _err(capsys)


@pytest.mark.parametrize("cwd, targets", [
    (".", ["sub/dvc.yaml:sbuild", "data/raw.dvc"]),
    (".", ["sub/out.csv"]),
    ("sub", ["dvc.yaml:sbuild"]),
    ("sub", ["out.csv"]),
], ids=["nested-key-from-root", "nested-path-from-root", "key-from-sub", "path-from-sub"])
def test_a_push_naming_a_nested_stage_goes_ahead_beside_stages_never_run(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys, cwd: str, targets: list[str],  # noqa: F811
) -> None:
    """A nested pipeline's stage, named by its key or by its out's path,
    from the root or from its own folder, resolves to that stage, so the
    stages that never ran elsewhere don't stop it.

    Mutations: read only root stage keys, or only the root lock's outs ->
    the target is unresolved, every pointer is checked, refused, red.
    """
    proj = _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=True)
    monkeypatch.chdir(proj / cwd)
    puts = len(s3_server.puts)

    assert cli.main(["data", "push", *targets]) == 0, _err(capsys)
    assert any(p.endswith("/sub/out.csv") for p in s3_server.puts[puts:])


def test_the_rest_names_only_stages_dvc_lists(tmp_path, monkeypatch, real_dvc, s3_server, capsys) -> None:  # noqa: F811
    """A lock entry whose stage dvc.yaml no longer declares isn't offered:
    dvc refuses `dvc.yaml:gone` ("Stage 'gone' not found").

    Mutation: offer lock entries instead of dvc's names -> the example
    names dvc.yaml:gone and fails, red.
    """
    _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=False, lock="orphan")

    assert cli.main(["data", "push"]) == 1
    rest = _command(_err(capsys), "for example ", "; if dvc.lock")
    assert "dvc.yaml:gone" not in rest
    assert cli.main(rest[1:]) == 0, _err(capsys)


@pytest.mark.parametrize("lock, dvc_says", [("broken", "Expected the node content"), ("int-key", "expected str")],
                         ids=["not-yaml", "a-stage-named-012"])
def test_a_dvc_yaml_dvc_cannot_read_stops_the_push_with_dvcs_message_once(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys, lock: str, dvc_says: str,  # noqa: F811
) -> None:
    """dvc can't list sub/dvc.yaml's stages (not YAML, or a stage named
    `012`, which YAML 1.2 reads as a number and dvc rejects), so mintd can't
    tell what a push of everything would skip: it refuses before any
    upload, saying to fix the file, with dvc's message printed once.

    Mutations: read an unreadable pipeline as having no stages -> the push
    goes ahead, red; stream the stage list -> dvc's message printed twice,
    red.
    """
    _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=False, lock=lock)
    puts = len(s3_server.puts)

    assert cli.main(["data", "push"]) == 1
    printed = capsys.readouterr()
    err = " ".join(printed.err.split())
    assert "nothing was written or pushed: dvc can't read sub/dvc.yaml, so mintd can't tell" in err, err
    assert "fix sub/dvc.yaml, then push" in err
    assert (printed.out + printed.err).count(dvc_says) == 1, printed
    assert len(s3_server.puts) == puts


def test_from_a_subfolder_both_hint_commands_run_where_mintd_did(
    tmp_path, monkeypatch, real_dvc, s3_server, capsys,  # noqa: F811
) -> None:
    """Run from sub/, the hints' commands are spelled from sub/ (`dvc repro
    dvc.yaml:sother`, `../dvc.yaml:build`), and each works there as
    printed.

    Mutation: spell them from the project root -> `sub/dvc.yaml` resolves
    to sub/sub/dvc.yaml, red.
    """
    proj = _pipeline(tmp_path, monkeypatch, real_dvc, s3_server, version_aware=False)
    monkeypatch.chdir(proj / "sub")

    assert cli.main(["data", "push", "sother"]) == 1
    repro = _command(_err(capsys), "hint: run ", ", then push")
    assert repro == ["dvc", "repro", "dvc.yaml:sother"]

    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    rest = _command(err, "for example ", "; if dvc.lock")
    assert rest[3:] == ["../dvc.yaml:build", "../dvc.yaml:fan@p q", "../dvc.yaml:mx@x"]
    assert cli.main(rest[1:]) == 0, _err(capsys)
    repro = _command(err, "hint: run ", ", then push")
    r = real_dvc(repro[1:], cwd=proj / "sub")
    assert r.returncode == 0, r.stderr
    assert cli.main(["data", "push"]) == 0, _err(capsys)
