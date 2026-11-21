"""The stories' recall run: undo a past fix, rebuild mintd, run the stories, see which go red.

By hand only (it refuses when `CI` is set), from a clean commit:

    uv run python -m tests.stories.run_recall [ROW ...]          # the rows, or those whose fix names a ROW
    uv run python -m tests.stories.run_recall --mutants 50       # the seeded mutation sample

How each build is made:
- **Rows:** a patch in `tests/stories/recall/` that undoes one fix on HEAD. HEAD is the control: the run starts
  with an unpatched HEAD, which must be green, so a red comes from the patch alone. Each patch's header names
  its fix, its pre-fix commit and the fix's own tests, which must pass at HEAD and fail with the patch applied
  before the row counts (checked in this tree, then in the build's clone; a parametrized test in at least one
  case). A row marked "Windows only"
  is not built elsewhere: it counts as missed, "Windows only, not run here", and stays in the bar's denominator.
- **Mutants:** one-line changes to `src/mintd` drawn with a fixed seed: a flipped comparison, a negated
  condition, a dropped return, an off-by-one. Each is built and run like a patch, stopping at its first red. One
  whose file no longer compiles is never drawn; one whose `mintd --version` fails is listed and left out of the
  share (any story would kill it); one that outlasts its wall limit is listed and counted killed; an error (a
  HarnessError, a fixture error) is listed and left out.

A verdict comes from pytest's junit report, never its exit code alone; this module, loaded into each story run
as a plugin, records every failure's exception type there. For a row, only an assertion (an AssertionError,
the harness's Mismatch included) is a catch: a timeout, a refused connection, a `pytest.fail`, a HarnessError,
a fixture error, a skip, no test at all, an unreadable report, an exit code other than 0 or 1, an exit 1 with
no failure, a failed build, a patch that no longer applies or a fix's test that stays green makes the row an
error, never a catch; the run then exits 1. A mutant is killed by any failure but a HarnessError, since a
mintd that hangs or crashes is broken too. Strict-xfail Targets are deselected (`-m "not slow and not
xfail"`), so an XPASS cannot read as red. Each build lives in its own temporary folder with pytest's basetemp
and is deleted after its run, Ctrl-C included, so one build at a time is on disk.

No stale bytecode: a patch or mutant is applied in the build's own clone of HEAD and installed as a wheel into a
fresh venv, never in this tree, so no `__pycache__` here holds a mutant; and the stories run with
`PYTHONDONTWRITEBYTECODE=1`, so they write none either. (A mutated file restored in place by a same-size write in
the same second keeps its stale `.pyc`, which is why nothing here edits the tree.)

The bar: at least 80% of the past-fix rows caught, and every data-loss row. The mutants' share is given with
its 95% Wilson interval.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

import pytest

from tests._harness import HarnessError
from tests._harness.git import _git
from tests._harness.mintd import REPO, build_mintd

STORIES = Path(__file__).resolve().parent
RECALL = STORIES / "recall"
PLUGIN = "tests.stories.run_recall"  # this module, loaded into each pytest run for its exception-type hook
MARK = "not slow and not xfail"
SEED = 20261119
MIN_FREE = 4 * 2**30


class Row(NamedTuple):
    fix: str
    #: a `.patch` in `recall/`, applied on HEAD
    patch: str
    #: the story files that must go red; () for every story
    stories: tuple[str, ...]
    data_loss: bool
    #: found after the stories were written: reported apart from the recall number
    after: bool = False


ROWS: tuple[Row, ...] = (
    # Past fixes: the recall number.
    Row("#60: ✓ over an import that landed nothing", "60-import-of-nothing-prints-tick.patch",
        ("test_story_1_import.py",), data_loss=True),
    Row("#65: a failed import left its folders", "65-failed-import-leaves-folders.patch",
        ("test_story_2_failed_import.py",), data_loss=True),
    Row("#68: one S3 5xx aborted the whole cache pull", "68-one-5xx-aborts-cache-pull.patch",
        ("test_story_9_cache.py",), data_loss=False),
    Row("#70: ✓ before storage held every pinned version", "70-tick-before-storage-holds-every-version.patch",
        ("test_story_3_push.py",), data_loss=True),
    Row("#74: gitignored pointers pushed or pulled", "74-gitignored-pointers-pushed-and-pulled.patch",
        ("test_story_4_ignored_pointers.py",), data_loss=True),
    Row("#70, found in review: ✓ over 0 files for a foreach group", "70r-vacuous-tick-on-a-foreach-group.patch",
        ("test_story_3_push.py",), data_loss=True),
    Row("#70, found in review: a pin proved by its size alone", "70r-size-only-proof.patch",
        ("test_story_3_push.py",), data_loss=True),
    Row("#70, found in review: a rollback that changed line endings (Windows)", "70r-crlf-rollback.patch",
        ("test_story_13_restart.py",), data_loss=False),
    Row("#61, found in review: ✓ when git cannot answer", "61r-tick-when-git-cannot-answer.patch",
        ("test_story_3_push.py",), data_loss=False),
    # Found after the stories were written: reported apart, against every story.
    Row("#97: push exits 0 on a pointer that does not parse", "97a-push-exits-0-on-a-pointer-that-does-not-parse.patch",
        (), data_loss=True, after=True),
    Row("#97: a traceback on a pointer cut at `cloud:`", "97b-traceback-on-a-pointer-cut-at-cloud.patch",
        (), data_loss=False, after=True),
    Row("#111: a folder pointer shorter than its folder pushes as a smaller folder",
        "111-folder-pointer-shorter-than-its-folder.patch", (), data_loss=True, after=True),
)


class Result(NamedTuple):
    verdict: str  # green | red | error | hung | no-start
    #: test id -> the first line of its failure (red), or what made it an error
    detail: dict[str, str]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> pytest.TestReport:
    """Loaded into each story run (`-p tests.stories.run_recall`): records a failure's exception type, and
    whether it is an assertion or a HarnessError, as properties of its junit testcase."""
    report = yield
    if report.failed and call.excinfo is not None:
        kind = (call.excinfo.type.__module__, call.excinfo.type.__qualname__)
        item.user_properties.append(("exception", ".".join(kind)))
        item.user_properties.append(("kind", "harness" if issubclass(call.excinfo.type, HarnessError)
                                     else "assertion" if issubclass(call.excinfo.type, AssertionError) else "other"))
    return report


def classify(junit: Path, rc: int, *, strict: bool) -> Result:
    """The verdict of one pytest run, from its junit report. `strict` (a row): only an assertion is red, any
    other exception an error. Otherwise (a mutant): any failure but a HarnessError is red."""
    if rc not in (0, 1):
        return Result("error", {"pytest": f"exit {rc}"})
    try:
        cases = list(ET.parse(junit).getroot().iter("testcase"))
    except (ET.ParseError, OSError) as e:
        return Result("error", {"pytest": f"no readable junit report ({type(e).__name__})"})
    red: dict[str, str] = {}
    wrong: dict[str, str] = {}
    for case in cases:
        name = f"{case.get('classname')}::{case.get('name')}"
        props = {p.get("name"): p.get("value") for p in case.iter("property")}
        for child in case:
            said = "".join((child.get("message") or child.text or "").strip().splitlines()[:1])
            if child.tag in ("error", "skipped"):
                wrong[name] = f"{child.tag}: {said}"
            elif child.tag == "failure":
                kind = props.get("kind")
                if kind == "assertion" or (kind == "other" and not strict):
                    red[name] = said
                else:  # a HarnessError; for a row, any exception but an assertion; or a failure the plugin missed
                    wrong[name] = f"{props.get('exception', 'unrecorded exception')}, not a catch: {said}"
    if not cases:
        return Result("error", {"pytest": "no test ran"})
    if wrong:
        return Result("error", wrong)
    if rc == 1 and not red:
        return Result("error", {"pytest": "exit 1 with no failure"})
    return Result("red" if red else "green", red)


def wilson(k: int, n: int) -> tuple[float, float]:
    """The 95% Wilson interval of `k` in `n`, in percent: never empty at 0 or n, unlike the normal one."""
    z2, p = 1.96**2, k / n
    mid, half = (p + z2 / (2 * n)) / (1 + z2 / n), 1.96 * (p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5 / (1 + z2 / n)
    return 100 * max(0.0, mid - half), 100 * min(1.0, mid + half)


def score(rows: list[Row], caught: set[str]) -> tuple[str, bool]:
    n, c = len(rows), sum(r.fix in caught for r in rows)
    loss = [r for r in rows if r.data_loss]
    d = sum(r.fix in caught for r in loss)
    met = n > 0 and 5 * c >= 4 * n and d == len(loss)
    pct = 100 * c // n if n else 0
    return (f"recall: {c} of {n} rows ({pct}%); data-loss rows: {d} of {len(loss)}; "
            f"bar (at least 80% and every data-loss row): {'met' if met else 'not met'}"), met


def recall_line(past: list[Row], caught: set[str], failed: set[str]) -> str:
    """The recall line: the bar's verdict over every past-fix row, or a count with none when only some ran
    or one errored (an error is not a miss)."""
    if len(past) == sum(not r.after for r in ROWS) and not failed & {r.fix for r in past}:
        return score(past, caught)[0]
    why = "a subset" if len(past) < sum(not r.after for r in ROWS) else f"{len(failed)} rows errored"
    return f"recall over the {len(past)} rows run: {sum(r.fix in caught for r in past)} caught ({why}: no bar verdict)"


# -- mutants -------------------------------------------------------------------------------------------------

_FLIPS: dict[type, type] = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}


class Site(NamedTuple):
    path: str  # repo-relative
    line: int  # 1-based
    col: int  # UTF-8 byte offsets within the line, as ast gives them
    end: int
    op: str
    before: str  # line[col:end] as it is
    after: str  # what replaces it


def _segment(lines: list[bytes], node: ast.AST) -> str:
    return lines[node.lineno - 1][node.col_offset:node.end_col_offset].decode("utf-8")  # type: ignore[attr-defined]


def mutant_sites(src: Path) -> list[Site]:
    """Every one-line mutation the sample draws from, in a stable order."""
    sites: list[Site] = []
    for file in sorted(src.rglob("*.py")):
        text = file.read_text(encoding="utf-8")
        lines = text.encode("utf-8").split(b"\n")
        rel = file.resolve().relative_to(REPO.resolve()).as_posix()
        tree = ast.parse(text)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def add(node: ast.AST, op: str, after: str, rel: str = rel, lines: list[bytes] = lines) -> None:
            if node.lineno == node.end_lineno:  # type: ignore[attr-defined]
                sites.append(Site(rel, node.lineno, node.col_offset, node.end_col_offset, op,  # type: ignore[attr-defined]
                                  _segment(lines, node), after))

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _FLIPS:
                flipped = ast.Compare(node.left, [_FLIPS[type(node.ops[0])]()], node.comparators)
                add(node, "flip", ast.unparse(flipped))
            elif isinstance(node, (ast.If, ast.While, ast.IfExp)) and not isinstance(node.test, ast.Compare):
                if "TYPE_CHECKING" not in ast.unparse(node.test):
                    add(node.test, "negate", f"not ({_segment(lines, node.test)})")
            elif isinstance(node, ast.Return):
                add(node, "drop-return", "pass")
            elif (isinstance(node, ast.Constant) and type(node.value) is int
                  and isinstance(parents.get(node), (ast.Compare, ast.Subscript, ast.Slice, ast.BinOp))):
                add(node, "off-by-one", str(node.value + 1))
            elif (isinstance(node, ast.Constant) and type(node.value) is int and isinstance(parents.get(node), ast.Call)
                  and isinstance(parents[node].func, ast.Name) and parents[node].func.id == "range"):  # type: ignore[attr-defined]
                add(node, "off-by-one", str(node.value + 1))
    return sorted(sites, key=lambda s: (s.path, s.line, s.col, s.op))


def mutate(site: Site) -> tuple[str, str]:
    """The file's text before and after `site`'s change."""
    before = (REPO / site.path).read_text(encoding="utf-8")
    lines = before.encode("utf-8").split(b"\n")
    line = lines[site.line - 1]
    lines[site.line - 1] = line[:site.col] + site.after.encode("utf-8") + line[site.end:]
    return before, b"\n".join(lines).decode("utf-8")


def mutant_patch(site: Site) -> str:
    before, after = mutate(site)
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"a/{site.path}", f"b/{site.path}"))


def draw(sites: list[Site], seed: int, n: int) -> list[Site]:
    """The first `n` sites, in a seeded shuffle, whose mutated file still compiles. A larger `n` keeps the
    same first mutants."""
    order = sites[:]
    random.Random(seed).shuffle(order)
    kept: list[Site] = []
    for site in order:
        if len(kept) == n:
            break
        try:
            compile(mutate(site)[1], site.path, "exec")
        except SyntaxError:
            continue
        kept.append(site)
    return kept


# -- runs ----------------------------------------------------------------------------------------------------

def regression_tests(patch: str) -> tuple[tuple[str, ...], bool]:
    """The fix's own tests a patch's header names (`validated: ...`), and whether they go red only on Windows."""
    line = next((ln for ln in patch.splitlines() if ln.startswith("validated")), "")
    return tuple(re.findall(r"tests/[\w/]+\.py::[\w\[\]-]+", line)), line.startswith("validated (Windows only)")


def unvalidated(junit: Path, rc: int, named: tuple[str, ...]) -> str | None:
    """Why the fix's own tests do not show the patch undoing it, or None when they do: each named test must
    fail, a parametrized one in at least one case (`name[...]`; some cases pass by design)."""
    got = classify(junit, rc, strict=False)
    ids = {f"{t.split('::')[0].removesuffix('.py').replace('/', '.')}::{t.split('::')[1]}": t for t in named}
    if not ids:
        return "its header names no test of the fix"
    if got.verdict != "red":
        return f"the fix's tests did not fail with the patch applied: {got.verdict} {got.detail}"
    green = sorted(t for i, t in ids.items() if not any(k == i or k.startswith(f"{i}[") for k in got.detail))
    return f"stayed green with the patch applied: {', '.join(green)}" if green else None


def _run(rev: str, patch: str | None, stories: tuple[str, ...], *, strict: bool, first_red: bool = False,
         timeout: float | None = None) -> Result:
    """Build mintd at `rev` (with `patch` applied), check that the fix's own tests a row patch names go red in
    the build's clone, and run `stories` against the build, in one temporary folder."""
    with tempfile.TemporaryDirectory(prefix="mintd-recall-") as tmp:
        root = Path(tmp)
        if shutil.disk_usage(root).free < MIN_FREE:
            raise SystemExit(f"under {MIN_FREE // 2**30} GiB free where builds go ({root.parent}): free space first")
        diff = None
        if patch is not None:
            diff = root / "undo.patch"
            diff.write_text(patch, encoding="utf-8")
        try:
            mintd = build_mintd(rev, root / "build", diff)
        except (HarnessError, subprocess.CalledProcessError, OSError) as e:
            return Result("error", {"build": str(e).splitlines()[0] if str(e) else type(e).__name__})
        if subprocess.run([str(mintd), "--version"], capture_output=True, check=False).returncode:
            return Result("no-start", {"mintd": "--version failed"})
        named = regression_tests(patch)[0] if patch is not None and strict else ()
        if named:
            clone, regression = root / "build" / "src", root / "regression.xml"
            # The clone's own tests, on the clone's patched src (PYTHONPATH comes before this tree's editable mintd).
            ran = subprocess.run(
                [sys.executable, "-m", "pytest", *named, "-q", "-p", "no:cacheprovider", "-p", PLUGIN,
                 f"--basetemp={root / 'regression'}", f"--junitxml={regression}"],
                cwd=clone, env={**os.environ, "PYTHONPATH": str(clone / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, check=False,
            )
            why = unvalidated(regression, ran.returncode, named)
            if why:
                return Result("error", {"validation": why})
        junit = root / "junit.xml"
        argv = [sys.executable, "-m", "pytest", *([str(STORIES / s) for s in stories] or [str(STORIES)]), "-q",
                "-m", MARK, "-p", "no:cacheprovider", "-p", PLUGIN, f"--basetemp={root / 'pytest'}",
                f"--junitxml={junit}", *(["-x"] if first_red else [])]
        with (root / "pytest.log").open("w") as log:
            # A session of its own: a Ctrl-C here reaches only this runner, which then stops pytest itself
            # and waits for it, so its teardown is done before the folder is deleted.
            proc = subprocess.Popen(
                argv, cwd=REPO, env={**os.environ, "MINTD_BIN": str(mintd), "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                rc = proc.wait(timeout)
            except BaseException as e:  # the wall limit, or Ctrl-C
                _stop(proc)
                if not isinstance(e, subprocess.TimeoutExpired):
                    raise
                return Result("hung", {"pytest": f"no exit within {timeout:.0f} s"})
        return classify(junit, rc, strict=strict)


def _stop(proc: subprocess.Popen) -> None:
    """Ctrl-C to pytest's group, so its KeyboardInterrupt kills each mintd's group; then up to 60 s for its
    teardown, waited out through any further Ctrl-C, before a kill. pytest has exited when this returns."""
    os.killpg(proc.pid, signal.SIGINT)
    deadline = time.monotonic() + 60
    while proc.poll() is None:
        try:
            proc.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except KeyboardInterrupt:  # another Ctrl-C: keep waiting, so the folder outlives pytest
            continue


def not_green_at_head(named: tuple[str, ...]) -> str | None:
    """Why the fix's tests do not all pass at HEAD (this tree, which `_unchanged` keeps at HEAD), or None.
    Only when they pass here does their failing in the patched build mean the patch broke them, not the
    environment (a missing `python` on PATH fails some of them anywhere)."""
    with tempfile.TemporaryDirectory(prefix="mintd-recall-") as tmp:
        junit = Path(tmp) / "head.xml"
        ran = subprocess.run(
            [sys.executable, "-m", "pytest", *named, "-q", "-p", "no:cacheprovider", "-p", PLUGIN,
             f"--basetemp={Path(tmp) / 'pytest'}", f"--junitxml={junit}"],
            cwd=REPO, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, check=False,
        )
        got = classify(junit, ran.returncode, strict=True)
    return None if got.verdict == "green" else (
        f"the fix's tests do not pass at HEAD, so their failing with the patch would prove nothing: "
        f"{got.verdict} {got.detail}")


def row_result(row: Row, head: str) -> Result:
    """A row's verdict. One whose defect shows only on Windows is not built elsewhere: it counts as missed
    and stays in the bar's denominator, so a row that cannot run here can never flip the bar to met. Before
    a row is built, the fix's tests its patch names must pass at HEAD."""
    patch = (RECALL / row.patch).read_text(encoding="utf-8")
    named, windows_only = regression_tests(patch)
    if windows_only and os.name != "nt":
        return Result("green", {"not run": "Windows only, not run here"})
    why = not_green_at_head(named)
    if why:
        return Result("error", {"validation": why})
    return _run(head, patch, row.stories, strict=True)


def preflight(rows: list[Row]) -> None:
    """Every patch applies to HEAD and names its fix's tests, and every story file collects a test: checked
    before any build, so a stale row stops the run instead of reading as a miss."""
    for row in rows:
        try:
            _git(["apply", "--check", str(RECALL / row.patch)], cwd=REPO)
        except HarnessError as e:
            raise SystemExit(f"{row.patch} no longer applies to HEAD; regenerate it: {e}") from None
        if not regression_tests((RECALL / row.patch).read_text(encoding="utf-8"))[0]:
            raise SystemExit(f"{row.patch}: its header's `validated:` line names no test of the fix")
        for story in row.stories or ("",):
            got = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", MARK,
                                  "-p", "no:cacheprovider", str(STORIES / story)],
                                 cwd=REPO, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                 capture_output=True, text=True, check=False)
            if got.returncode:
                raise SystemExit(f"{row.fix}: {story or 'the stories'} collect no test under -m {MARK!r}")


def _line(cells: list[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def _detail(got: Result) -> str:
    return "; ".join(f"{k.rsplit('::', 1)[-1]}: {v[:120]}" for k, v in got.detail.items())


def _unchanged(head: str) -> None:
    """The stories, the harness and the patches are read from this tree for hours, while the builds start
    from `head`: refuse unless the tree is still `head`, untouched."""
    if _git(["rev-parse", "HEAD"], cwd=REPO, host=True).strip() != head or _git(
            ["status", "--porcelain", "--untracked-files=normal", "--", "src", "tests", "pyproject.toml", "uv.lock"],
            cwd=REPO, host=True):
        raise SystemExit(f"commit first, and leave the tree alone while this runs: the result must name {head[:7]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.stories.run_recall", description=__doc__.splitlines()[0])
    ap.add_argument("rows", nargs="*", help="run only the rows whose fix contains one of these")
    ap.add_argument("--mutants", type=int, default=0, metavar="N", help="run the seeded sample of N mutants instead")
    args = ap.parse_args(argv)
    if os.environ.get("CI"):
        raise SystemExit("the recall run is by hand only, and CI is set")
    head = _git(["rev-parse", "HEAD"], cwd=REPO).strip()
    _unchanged(head)
    rows = [] if args.mutants else [r for r in ROWS if not args.rows or any(s in r.fix for s in args.rows)]
    preflight(rows)
    started = time.monotonic()
    base = _run(head, None, (), strict=True)
    if base.verdict != "green":
        raise SystemExit(f"HEAD {head[:7]} is not green, so no red can be read: {base.verdict} {base.detail}")
    took = time.monotonic() - started
    print(f"HEAD {head[:7]}: every story green in {took:.0f} s", file=sys.stderr)
    errors = 0
    print(f"## Recall at {head[:7]}\n")
    if rows:
        caught: set[str] = set()
        failed: set[str] = set()
        for after in (False, True):
            part = [r for r in rows if r.after is after]
            if not part:
                continue
            print("Found after the stories were written:\n" if after else "Past fixes:\n")
            print(_line(["Fix", "Build", "Stories", "Data loss", "Result", "Red tests"]))
            print(_line(["---"] * 6))
            for row in part:
                print(f"{row.fix} ...", file=sys.stderr)
                _unchanged(head)
                got = row_result(row, head)
                verdict = got.verdict
                errors += verdict not in ("red", "green")
                if verdict not in ("red", "green"):
                    failed.add(row.fix)
                if verdict == "red":
                    caught.add(row.fix)
                shown = "caught" if verdict == "red" else "missed" if verdict == "green" else verdict
                print(_line([row.fix, row.patch[:40], ", ".join(row.stories) or "every story",
                             "yes" if row.data_loss else "no", shown, _detail(got)]))
            print()
        _unchanged(head)  # the last row ran on the tree this names
        past = [r for r in rows if not r.after]
        if past:
            print(recall_line(past, caught, failed))
        later = [r for r in rows if r.after]
        if later:
            print(f"after the stories: {sum(r.fix in caught for r in later)} of {len(later)} caught")
    if args.mutants:
        sites = draw(mutant_sites(REPO / "src" / "mintd"), SEED, args.mutants)
        counts = {"red": 0, "green": 0, "hung": 0, "no-start": 0, "error": 0}
        print(_line(["Where", "Op", "Change", "Result", "Detail"]))
        print(_line(["---"] * 5))
        for i, site in enumerate(sites, 1):
            print(f"mutant {i}/{len(sites)}: {site.path}:{site.line} {site.op}", file=sys.stderr)
            _unchanged(head)
            got = _run(head, mutant_patch(site), (), strict=False, first_red=True, timeout=3 * took + 600)
            counts[got.verdict] += 1
            errors += got.verdict == "error"
            shown = {"red": "killed", "green": "survived", "hung": "killed (hung)"}.get(got.verdict, got.verdict)
            print(_line([f"{site.path}:{site.line}", site.op, f"`{site.before[:50]}` → `{site.after[:50]}`", shown,
                         _detail(got)]))
        _unchanged(head)  # the last mutant ran on the tree this names
        started_ = counts["red"] + counts["green"] + counts["hung"]
        killed = counts["red"] + counts["hung"]
        share = (f"{100 * killed / started_:.0f}%, 95% interval {wilson(killed, started_)[0]:.0f}% to "
                 f"{wilson(killed, started_)[1]:.0f}%" if started_ else "no mutant started")
        print(f"\nmutants: killed {killed} of {started_} started ({share}); seed {SEED}, "
              f"{len(sites)} drawn; {counts['no-start']} did not start; {counts['hung']} hung (counted killed); "
              f"{counts['error']} errors (excluded)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
