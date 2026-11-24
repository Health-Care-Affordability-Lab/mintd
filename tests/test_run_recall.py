"""The recall runner's own rules (`tests/stories/run_recall.py`): how a run's verdict is read, what counts as
a catch, the bar, and the mutants it draws. No build, no story run."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.stories import run_recall as rr


def _junit(tmp_path: Path, *cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{"".join(cases)}</testsuite></testsuites>',
                    encoding="utf-8")
    return path


def _case(name: str, child: str = "", kind: str | None = None) -> str:
    props = f'<properties><property name="kind" value="{kind}"/></properties>' if kind else ""
    return f'<testcase classname="tests.stories.test_story_x" name="{name}">{props}{child}</testcase>'


_FAILURES = """
import subprocess
import pytest
from tests._harness import HarnessError


class GivenBroke(HarnessError):
    pass


def test_assert():
    assert "✓" not in "✓ done"


def test_timeout():
    raise subprocess.TimeoutExpired("mintd", 300)


def test_refused():
    raise ConnectionRefusedError(61, "Connection refused")


def test_called():
    raise subprocess.CalledProcessError(1, "git")


def test_failed():
    pytest.fail("no pinned file")


def test_harness_subclass():
    raise GivenBroke("the Given broke")
"""


def test_only_an_assertion_is_a_catch_for_a_row_and_a_harness_error_never_is(tmp_path: Path) -> None:
    """Real junit, from pytest run with the runner loaded as a plugin. Mutations that turn it red: a row
    counting any failure as a catch (the timeout, the refused connection, the failed subprocess and
    `pytest.fail` all read red); a HarnessError subclass read as red; the plugin not recording the type."""
    (tmp_path / "test_failures.py").write_text(_FAILURES, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    junit = tmp_path / "junit.xml"
    ran = subprocess.run([sys.executable, "-m", "pytest", str(tmp_path / "test_failures.py"), "-q", "-c",
                          str(tmp_path / "pytest.ini"), "-p", "no:cacheprovider", "-p", rr.PLUGIN,
                          f"--junitxml={junit}"],
                         cwd=Path(__file__).parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                         capture_output=True, text=True, check=False)
    row = rr.classify(junit, ran.returncode, strict=True)
    assert row.verdict == "error", ran.stdout
    named = {k.rsplit("::", 1)[-1]: v for k, v in row.detail.items()}
    assert set(named) == {"test_timeout", "test_refused", "test_called", "test_failed", "test_harness_subclass"}
    assert named["test_timeout"].startswith("subprocess.TimeoutExpired, not a catch")
    mutant = rr.classify(junit, ran.returncode, strict=False)
    assert mutant == rr.Result("error", {k: v for k, v in row.detail.items() if k.endswith("test_harness_subclass")})
    alone = rr.classify(_junit(tmp_path, _case("test_assert", '<failure message="assert 1"/>', "assertion")),
                        1, strict=True)
    assert alone.verdict == "red"


def test_a_report_that_cannot_be_read_or_disagrees_with_the_exit_code_is_an_error(tmp_path: Path) -> None:
    """Mutations that turn it red: an `<error>` or a skip counted as red; a failure the plugin did not type
    counted as a catch; no test, an exit 5, a missing, empty or malformed report, or an exit 1 with no
    failure read as green (or crashing the run)."""
    red = _case("t1", '<failure message="assert 1 == 0">boom</failure>', "assertion")
    assert rr.classify(_junit(tmp_path, red, _case("t2")), 1, strict=True) == rr.Result(
        "red", {"tests.stories.test_story_x::t1": "assert 1 == 0"})
    assert rr.classify(_junit(tmp_path, _case("t1"), _case("t2")), 0, strict=True).verdict == "green"
    for broken in (_case("t2", '<error message="fixture setup failed"/>'), _case("t2", '<skipped message="no"/>'),
                   _case("t2", '<failure message="assert 1"/>')):
        for strict in (True, False):
            assert rr.classify(_junit(tmp_path, red, broken), 1, strict=strict).verdict == "error", broken
    assert rr.classify(_junit(tmp_path), 0, strict=True).verdict == "error"  # no test ran
    assert rr.classify(_junit(tmp_path, red, _case("t2")), 5, strict=True).verdict == "error"
    assert rr.classify(_junit(tmp_path, red), 2, strict=True).verdict == "error"
    assert rr.classify(_junit(tmp_path, _case("t1")), 1, strict=True).verdict == "error"  # exit 1, no failure
    assert rr.classify(tmp_path / "missing.xml", 1, strict=True).verdict == "error"
    for text in ("", "<testsuites><testsuite>"):
        (tmp_path / "bad.xml").write_text(text, encoding="utf-8")
        assert rr.classify(tmp_path / "bad.xml", 1, strict=True).verdict == "error"


def test_a_row_counts_only_when_every_test_its_patch_names_goes_red(tmp_path: Path) -> None:
    """Mutations that turn it red: a named test that stayed green let through; a header naming no test
    accepted; the Windows-only mark not read. Every shipped patch names at least one test that exists."""
    named = ("tests/test_story_x.py::test_a", "tests/test_story_x.py::test_b")
    both = _junit(tmp_path, _case("test_a", '<failure message="assert 1"/>', "assertion"),
                  _case("test_b", '<failure message="assert 2"/>', "assertion"))
    both.write_text(both.read_text(encoding="utf-8").replace("tests.stories.test_story_x", "tests.test_story_x"),
                    encoding="utf-8")
    assert rr.unvalidated(both, 1, named) is None
    both.write_text(both.read_text(encoding="utf-8").replace('<failure message="assert 2"/>', ""), encoding="utf-8")
    assert "stayed green" in (rr.unvalidated(both, 1, named) or "")
    assert rr.unvalidated(both, 1, ()) is not None
    assert rr.regression_tests("validated (Windows only): tests/a.py::t goes red.") == (("tests/a.py::t",), True)
    for row in rr.ROWS:
        tests, _ = rr.regression_tests((rr.RECALL / row.patch).read_text(encoding="utf-8"))
        assert tests and all((rr.REPO / t.split("::")[0]).is_file() for t in tests), row.patch


def test_a_parametrized_fix_test_counts_when_any_of_its_cases_fails(tmp_path: Path) -> None:
    """Mutations that turn it red: matching only the bare id (every parametrized test reads green); requiring
    every case to fail; a prefix match without the bracket (test_ab's failure validating test_a)."""
    junit = _junit(tmp_path, _case("test_a[new]", '<failure message="assert 1"/>', "assertion"), _case("test_a[old]"),
                   _case("test_ab", '<failure message="assert 2"/>', "assertion"))
    junit.write_text(junit.read_text(encoding="utf-8").replace("tests.stories.test_story_x", "tests.test_story_x"),
                     encoding="utf-8")
    assert rr.unvalidated(junit, 1, ("tests/test_story_x.py::test_a",)) is None
    assert rr.unvalidated(junit, 1, ("tests/test_story_x.py::test_ab",)) is None
    junit.write_text(junit.read_text(encoding="utf-8").replace('name="test_a[new]"><properties>'
                                                               '<property name="kind" value="assertion"/></properties>'
                                                               '<failure message="assert 1"/>', 'name="test_a[new]">'),
                     encoding="utf-8")
    assert "stayed green" in (rr.unvalidated(junit, 1, ("tests/test_story_x.py::test_a",)) or "")


_FAKE_PYTEST = """#!{python}
import os, pathlib, sys
junit = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--junitxml="))
body = (pathlib.Path(os.environ["FAKE_REPORTS"]) / pathlib.Path(junit).name).read_text(encoding="utf-8")
pathlib.Path(junit).write_text(body, encoding="utf-8")
sys.exit(1 if "<failure" in body else 0)
"""


@pytest.mark.skipif(os.name == "nt", reason="a shebang fake")
def test_a_row_run_checks_the_fix_tests_first_and_reads_its_stories_strictly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stubbed build and a fake pytest that writes the junit it is given. Mutations that turn it red: the
    validation gate dropped or inverted in `_run`; `_run` reading a row's stories without `strict` (a timeout
    read as a catch)."""
    fake_mintd = tmp_path / "mintd"
    fake_mintd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_mintd.chmod(0o755)
    stub = tmp_path / "python"
    stub.write_text(_FAKE_PYTEST.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    reports = tmp_path / "reports"
    reports.mkdir()

    def build(rev: str, root: Path, patch: Path | None = None) -> Path:
        (root / "src").mkdir(parents=True)
        return fake_mintd

    monkeypatch.setattr(rr, "build_mintd", build)
    monkeypatch.setattr(rr.sys, "executable", str(stub))
    monkeypatch.setenv("FAKE_REPORTS", str(reports))
    patch = "validated: with this patch on HEAD, tests/test_x.py::test_fix goes red.\n"

    def report(name: str, *cases: str) -> None:
        name = {"stories": "junit"}.get(name, name)
        (reports / f"{name}.xml").write_text(
            _junit(tmp_path, *cases).read_text(encoding="utf-8").replace("tests.stories.test_story_x", "tests.test_x"),
            encoding="utf-8")

    report("regression", _case("test_fix[one]", '<failure message="assert 1"/>', "assertion"))
    report("stories", _case("test_story", '<failure message="assert ticked"/>', "assertion"))
    assert rr._run("HEAD", patch, (), strict=True).verdict == "red"
    report("stories", _case("test_story", '<failure message="TimeoutExpired"/>', "other"))
    assert rr._run("HEAD", patch, (), strict=True).verdict == "error"  # a timeout is no catch for a row
    report("stories", _case("test_story", '<failure message="assert ticked"/>', "assertion"))
    report("regression", _case("test_fix[one]"))
    got = rr._run("HEAD", patch, (), strict=True)
    assert got.verdict == "error" and "did not fail" in got.detail["validation"]


def test_a_row_runs_strictly_and_a_windows_only_row_is_a_miss_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations that turn it red: a row run without `strict` (the P1's false catches back); a Windows-only
    row built off Windows, where a noise red could flip the bar to met; that row dropped from the count."""
    calls: list[dict] = []
    monkeypatch.setattr(rr, "_run", lambda rev, patch, stories, **kw: calls.append(kw) or rr.Result("red", {}))
    monkeypatch.setattr(rr, "not_green_at_head", lambda named: None)
    crlf = next(r for r in rr.ROWS if "crlf" in r.patch)
    monkeypatch.setattr(rr.os, "name", "posix")
    assert rr.row_result(crlf, "HEAD") == rr.Result("green", {"not run": "Windows only, not run here"})
    assert calls == []
    assert rr.row_result(rr.ROWS[0], "HEAD").verdict == "red"
    assert calls == [{"strict": True}]
    past = [r for r in rr.ROWS if not r.after]
    assert "7 of 9" in rr.recall_line(past, {r.fix for r in past[:7]}, set())  # the miss stays in the denominator


def test_the_mutant_interval_is_wilson() -> None:
    """Mutation that turns it red: the normal interval (7 of 50 gives 4% to 24%, and 0 of 50 gives ± 0)."""
    assert [round(x) for x in rr.wilson(7, 50)] == [7, 26]
    low, high = rr.wilson(0, 50)
    assert low == 0 and round(high) == 7
    assert round(rr.wilson(50, 50)[0]) == 93 and rr.wilson(50, 50)[1] == 100


def test_the_bar_is_four_in_five_rows_and_every_data_loss_row() -> None:
    """Mutations that turn it red: `>` for `>=`; the data-loss clause dropped."""
    rows = [rr.Row(f"#{i}", "x.patch", (), data_loss=i < 2) for i in range(5)]
    assert rr.score(rows, {"#0", "#1", "#2", "#3"})[1] is True
    assert rr.score(rows, {"#1", "#2", "#3", "#4"})[1] is False  # misses a data-loss row
    assert rr.score(rows[:4], {"#0", "#1", "#2"})[1] is False  # 3 of 4
    assert rr.score([], set())[1] is False


def test_mutants_splice_bytes_compile_and_draw_the_same_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations that turn it red: splicing the str instead of its bytes (the ✓ before it shifts the column);
    an unseeded shuffle."""
    src = tmp_path / "src" / "mintd"
    src.mkdir(parents=True)
    (src / "m.py").write_text('def f(a, b):\n    print("✓"); x = a == b\n    if a:\n        return x[1]\n'
                              '    return range(3)\n', encoding="utf-8")
    monkeypatch.setattr(rr, "REPO", tmp_path)
    sites = rr.mutant_sites(src)
    assert {s.op for s in sites} == {"flip", "negate", "drop-return", "off-by-one"}
    flip = next(s for s in sites if s.op == "flip")
    assert rr.mutate(flip)[1].splitlines()[1] == '    print("✓"); x = a != b'
    assert (flip.before, flip.after) == ("a == b", "a != b")
    for site in sites:
        before, after = rr.mutate(site)
        changed = [n for n, (x, y) in enumerate(zip(before.splitlines(), after.splitlines(), strict=True)) if x != y]
        assert len(changed) == 1, site
        compile(after, site.path, "exec")
    assert rr.draw(sites, rr.SEED, 3) == rr.draw(sites, rr.SEED, 3) == rr.draw(sites, rr.SEED, 5)[:3]


def test_a_subset_of_rows_or_an_errored_row_gets_no_bar_verdict() -> None:
    """Mutations that turn it red: the bar printed over whatever rows ran, or over a run in which a row
    errored (an error is not a miss); the full run losing its verdict."""
    past = [r for r in rr.ROWS if not r.after]
    full = rr.recall_line(past, {r.fix for r in past}, set())
    assert "bar (at least 80%" in full and "no bar verdict" not in full
    assert "a subset: no bar verdict" in rr.recall_line(past[:1], {past[0].fix}, set())
    assert "rows errored: no bar verdict" in rr.recall_line(past, {r.fix for r in past[1:]}, {past[0].fix})


def test_the_run_stops_when_the_tree_is_no_longer_the_commit_it_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation that turns it red: `_unchanged` not comparing HEAD, or not reading the status."""
    answers = {"rev-parse": "b" * 40, "status": ""}
    monkeypatch.setattr(rr, "_git", lambda args, cwd=None, host=False: answers[args[0]])
    with pytest.raises(SystemExit):
        rr._unchanged("a" * 40)
    answers.update({"rev-parse": "a" * 40, "status": " M tests/stories/test_story_3_push.py"})
    with pytest.raises(SystemExit):
        rr._unchanged("a" * 40)
    answers["status"] = ""
    rr._unchanged("a" * 40)


@pytest.mark.skipif(os.name == "nt", reason="process groups")
def test_a_run_past_its_wall_limit_stops_pytest_and_leaves_no_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake mintd, and a fake pytest that notes the Ctrl-C it gets. Mutations that turn it red: pytest
    started in the runner's own session (no group to signal); a kill with no Ctrl-C first (its teardown, and
    each mintd's, never runs)."""
    fake = tmp_path / "mintd"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    stub = tmp_path / "python"
    stub.write_text(f'#!/bin/sh\ntrap "echo stopped > {tmp_path / "mark"}; exit 2" INT\nsleep 30 &\nwait\n',
                    encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(rr, "build_mintd", lambda rev, root, patch=None: fake)
    monkeypatch.setattr(rr.sys, "executable", str(stub))
    monkeypatch.setattr(rr.tempfile, "tempdir", str(tmp_path / "tmp"))

    got = rr._run("HEAD", None, (), strict=True, timeout=1)

    assert got.verdict == "hung"
    assert (tmp_path / "mark").read_text(encoding="utf-8") == "stopped\n"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_build_s_stories_get_no_bucket_name_and_no_aws_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a bucket name, key files, a profile and boto/fsspec settings exported, the stories each build runs get
    none of them, so a story that can write to real storage by hand stays on the emulator. Mutation that turns it
    red: `_run`'s stories environment back to `{**os.environ, ...}`."""
    fake = tmp_path / "mintd"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    stub = tmp_path / "python"
    stub.write_text(f"#!/bin/sh\nenv > {tmp_path / 'env'}\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(rr, "build_mintd", lambda rev, root, patch=None: fake)
    monkeypatch.setattr(rr.sys, "executable", str(stub))
    planted = {"MINTD_STORY_BUCKET": "a-real-bucket", "AWS_SHARED_CREDENTIALS_FILE": "/home/op/.aws/key-file",
               "AWS_CONFIG_FILE": "/home/op/.aws/key-config", "AWS_PROFILE": "op", "AWS_ACCESS_KEY_ID": "AKIAOP",
               "BOTO_CONFIG": "/home/op/.boto", "FSSPEC_S3_KEY": "AKIAOP"}
    for k, v in planted.items():
        monkeypatch.setenv(k, v)

    rr._run("HEAD", None, (), strict=True)

    seen = dict(line.split("=", 1) for line in (tmp_path / "env").read_text(encoding="utf-8").splitlines() if "=" in line)
    assert not planted.keys() & seen.keys(), sorted(planted.keys() & seen.keys())
    assert not [k for k in seen if k.startswith(("AWS_", "BOTO_", "FSSPEC_"))]
    assert seen["MINTD_BIN"] == str(fake) and seen["PYTHONDONTWRITEBYTECODE"] == "1"


def test_preflight_writes_no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation that turns it red: preflight's collect-only run without PYTHONDONTWRITEBYTECODE."""
    seen: list[dict] = []
    monkeypatch.setattr(rr, "_git", lambda args, cwd=None, host=False: "")
    monkeypatch.setattr(rr.subprocess, "run", lambda argv, **kw: seen.append(kw.get("env") or {})
                        or rr.subprocess.CompletedProcess(argv, 0, "", ""))
    rr.preflight([rr.ROWS[2]])
    assert seen and all(env.get("PYTHONDONTWRITEBYTECODE") == "1" for env in seen)


def test_preflight_refuses_a_patch_whose_header_names_no_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation that turns it red: preflight not reading the header's `validated:` line."""
    (tmp_path / "x.patch").write_text("recall: undoes something.\nfix: abc.\n\ndiff --git a/x b/x\n", encoding="utf-8")
    monkeypatch.setattr(rr, "RECALL", tmp_path)
    monkeypatch.setattr(rr, "_git", lambda args, cwd=None, host=False: "")
    with pytest.raises(SystemExit, match="names no test"):
        rr.preflight([rr.Row("#0", "x.patch", ("test_story_9_cache.py",), data_loss=False)])


@pytest.mark.skipif(os.name == "nt", reason="a shebang fake")
def test_a_row_whose_fix_tests_fail_at_head_is_an_error_and_never_built(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake pytest stands in for the HEAD run of the fix's tests. Mutations that turn it red: the HEAD
    check dropped from `row_result`, or reading its failure as a pass (an environment failure in both runs
    would then pass validation)."""
    stub = tmp_path / "python"
    stub.write_text(_FAKE_PYTEST.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(rr.sys, "executable", str(stub))
    monkeypatch.setenv("FAKE_REPORTS", str(reports))
    built: list[str] = []
    monkeypatch.setattr(rr, "_run", lambda rev, patch, stories, **kw: built.append(rev) or rr.Result("red", {}))
    head = _junit(tmp_path, _case("test_import_whose_transfer_fails_leaves_no_folders",
                                  '<failure message="ReproductionError: failed to reproduce"/>', "other"))
    (reports / "head.xml").write_text(head.read_text(encoding="utf-8"), encoding="utf-8")
    got = rr.row_result(rr.ROWS[1], "HEAD")  # #65
    assert got.verdict == "error" and "do not pass at HEAD" in got.detail["validation"] and built == []
    (reports / "head.xml").write_text(_junit(tmp_path, _case("test_x")).read_text(encoding="utf-8"), encoding="utf-8")
    assert rr.row_result(rr.ROWS[1], "HEAD").verdict == "red" and built == ["HEAD"]
