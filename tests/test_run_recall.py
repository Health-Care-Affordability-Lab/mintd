"""The recall runner's own rules (`tests/stories/run_recall.py`): how a run's verdict is read, what counts as
a catch, the bar, and the mutants it draws. No build, no story run."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.stories import run_recall as rr


def _junit(tmp_path: Path, *cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{"".join(cases)}</testsuite></testsuites>',
                    encoding="utf-8")
    return path


def _case(name: str, child: str = "") -> str:
    return f'<testcase classname="tests.stories.test_story_x" name="{name}">{child}</testcase>'


def test_a_verdict_is_read_from_junit_and_only_an_assertion_is_a_catch(tmp_path: Path) -> None:
    """Mutations that turn it red: an `<error>`, a skip or a HarnessError counted as red; a real assertion
    whose traceback shows a `raise HarnessError` line counted as an error; a run with no test, an exit 5 or a
    missing report read as green."""
    red = _case("t1", '<failure message="AssertionError: assert 1 == 0">boom</failure>')
    assert rr.classify(_junit(tmp_path, red, _case("t2")), 1) == rr.Result(
        "red", {"tests.stories.test_story_x::t1": "AssertionError: assert 1 == 0"})
    assert rr.classify(_junit(tmp_path, _case("t1"), _case("t2")), 0).verdict == "green"
    for broken in (_case("t2", '<error message="fixture setup failed"/>'), _case("t2", '<skipped message="no"/>'),
                   _case("t2", '<failure message="tests._harness.HarnessError: git failed"/>')):
        assert rr.classify(_junit(tmp_path, red, broken), 1).verdict == "error", broken
    # pytest's text holds the test's source: a `raise HarnessError` line above a real assertion is no error
    beside = _case("t3", '<failure message="AssertionError: mintd printed a tick">'
                         'if not held:\n    raise HarnessError("no upload was refused")\n&gt; assert not ticked</failure>')
    assert rr.classify(_junit(tmp_path, beside), 1).verdict == "red"
    assert rr.classify(_junit(tmp_path), 0).verdict == "error"  # no test ran
    assert rr.classify(_junit(tmp_path), 5).verdict == "error"
    assert rr.classify(_junit(tmp_path, red, _case("t2")), 5).verdict == "error"  # exit 5 with tests in the report
    assert rr.classify(tmp_path / "missing.xml", 1).verdict == "error"
    assert rr.classify(_junit(tmp_path, red), 2).verdict == "error"


def test_a_commit_row_counts_only_the_reds_its_control_does_not_share() -> None:
    """Mutation that turns it red: the control ignored."""
    defect = rr.Result("red", {"a": "x", "b": "y"})
    assert rr.attributable(defect, rr.Result("red", {"b": "y"})) == {"a"}
    assert rr.attributable(defect, rr.Result("green", {})) == {"a", "b"}
    assert rr.attributable(rr.Result("green", {}), rr.Result("green", {})) == set()


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

    got = rr._run("HEAD", None, (), timeout=1)

    assert got.verdict == "hung"
    assert (tmp_path / "mark").read_text(encoding="utf-8") == "stopped\n"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_preflight_writes_no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation that turns it red: preflight's collect-only run without PYTHONDONTWRITEBYTECODE."""
    seen: list[dict] = []
    monkeypatch.setattr(rr, "_git", lambda args, cwd=None, host=False: "")
    monkeypatch.setattr(rr.subprocess, "run", lambda argv, **kw: seen.append(kw.get("env") or {})
                        or rr.subprocess.CompletedProcess(argv, 0, "", ""))
    rr.preflight([rr.Row("#0", "x.patch", ("test_story_9_cache.py",), data_loss=False)])
    assert seen and all(env.get("PYTHONDONTWRITEBYTECODE") == "1" for env in seen)
