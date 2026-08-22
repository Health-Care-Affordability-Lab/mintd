"""Substrate rules for the test suite itself (unit 1, S0).

These are ratchets, not correctness tests. Each pins a property of *how this
suite is written* against a checked-in literal, so the only way to move is to
edit the literal — which shows up in review — and the only cheap direction is
down. Rules 1 and 4 of the four substrate rules in
``notes/mintd-check/PLAN-hermetic-harness.md``.
"""

from __future__ import annotations

import ast
import collections
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"


# ---------------------------------------------------------------------------
# Rule 1 — doubles stand in only for process/network boundaries
# ---------------------------------------------------------------------------

# Composition-root seams: patching where the app wires its collaborators is how
# a test injects a fake at all, so these are permitted by construction.
_COMPOSITION_ROOT_PREFIX = "mintd.cli._resolve_"
_COMPOSITION_ROOT_EXACT = "mintd.cli._build_reporter"

# Process / network boundaries: the only things a double may stand in for.
_BOUNDARY_SUFFIXES = (
    "subprocess.run",
    "os.replace",
    "time.sleep",
    "_create_s3_client",
    "SubprocessDvcOps",
    "SubprocessInitOps",
    "GitCatalogClient",
    "importlib.metadata.version",
)

# BANNED: internal mintd functions and module attributes stubbed out
# wholesale (``_fast_sync_ops.boto3`` is the one attribute). 132 sites / 28
# targets at 70a7a9e, unmoved since. Shrink-only. The running totals for the
# WHOLE census (banned + permitted) live in
# ``test_the_checked_in_literal_matches_a_fresh_scan``, not here — a count
# repeated in a comment is a count that rots, which is this file's own thesis.
BANNED_TARGETS: dict[str, int] = {
    "mintd._config.Config.load": 1,
    "mintd._fast_sync_ops._check_dvc": 16,
    "mintd._fast_sync_ops.boto3": 1,
    "mintd.check.ProducerView.try_at": 1,
    "mintd.check.check_project": 6,
    "mintd.cli.Config.load": 11,
    "mintd.cli.bump_import": 6,
    "mintd.cli.check_project": 7,
    "mintd.cli.clone_and_pull_product": 9,
    "mintd.cli.config_ops.render_validation": 1,
    "mintd.cli.config_ops.validate_config": 2,
    "mintd.cli.data_pull": 5,
    "mintd.cli.enclave_bump": 6,
    "mintd.cli.enclave_package": 9,
    "mintd.cli.enclave_pull": 7,
    "mintd.cli.enclave_verify": 4,
    "mintd.cli.import_product": 2,
    "mintd.data.ProducerView.at": 1,
    "mintd.data.ProducerView.at_head": 1,
    "mintd.data.check_project": 2,
    "mintd.data.data_pull": 2,
    "mintd.data_ops.discover_all_outs": 15,
    "mintd.data_ops.partition_pipeline_outs": 7,
    "mintd.enclave.ProducerView.at_head": 1,
    "mintd.enclave.check_project": 1,
    "mintd.init._prompt_classification": 1,
    "mintd.init.render_scaffold": 1,
    "mintd.publish.check_project": 6,
}

# PERMITTED: the other half of the same census — composition root (32 sites /
# 8 targets) plus process/network boundary (30 sites / 11 targets). Pinned so
# the classifier above cannot be widened to launder a banned target.
PERMITTED_TARGETS: dict[str, int] = {
    "mintd.cli._build_reporter": 1,
    "mintd.cli._resolve_cache_ops": 2,
    "mintd.cli._resolve_catalog_client": 6,
    "mintd.cli._resolve_dvc_ops": 4,
    "mintd.cli._resolve_fast_sync_ops": 6,
    "mintd.cli._resolve_git_ops": 9,
    "mintd.cli._resolve_s3_listing_ops": 1,
    "mintd._aws_credentials.os.replace": 1,
    "mintd._fast_sync_ops._create_s3_client": 5,
    "mintd._init_ops.subprocess.run": 1,
    "mintd._fast_sync_ops.subprocess.run": 1,
    "mintd._fast_sync_ops.time.sleep": 9,
    "mintd._producer_git_ops.subprocess.run": 6,
    "mintd._share_ops._create_s3_client": 2,
    "mintd._templates._render.importlib.metadata.version": 1,
    "mintd.cli.GitCatalogClient": 2,
    "mintd.cli.SubprocessDvcOps": 2,
    "mintd.init.SubprocessInitOps": 1,
    "mintd.producer.os.replace": 1,
}


_DOUBLE_INJECTORS = frozenset({"setattr", "patch"})


def _scan_double_targets() -> dict[str, int]:
    """Every string-form double injected at a ``mintd.`` target under
    ``tests/``, by target.

    Matches the *callee name* — ``setattr`` or ``patch`` — with a string first
    argument, regardless of receiver. Both mechanisms this suite actually uses
    count: ``monkeypatch.setattr(...)``, the
    ``with monkeypatch.context() as crashing: crashing.setattr(...)`` form, and
    ``unittest.mock.patch(...)`` whether imported bare or as ``mock.patch``.

    Neither half is optional. Restricting the receiver to ``monkeypatch``
    silently drops ``tests/test_pre_units_journey.py:286``; dropping ``patch``
    silently drops 30 sites, 17 of them banned-class, and leaves the ratchet
    evadable by a one-word change of mechanism.
    """
    counts: collections.Counter[str] = collections.Counter()
    for path in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # SCOPE HOLE CLOSED (1b): the match was by callee NAME, so
        # ``from unittest.mock import patch as _p`` renamed its way straight
        # past this ratchet. Bind aliases per module and count them too. No
        # test aliases either injector today (measured: zero), so this closes
        # the evasion without moving a literal — the point is that it stays
        # closed when someone tries it.
        injectors = set(_DOUBLE_INJECTORS)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                injectors.update(
                    alias.asname
                    for alias in node.names
                    if alias.asname and alias.name in _DOUBLE_INJECTORS
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                callee = func.id
            elif isinstance(func, ast.Attribute):
                callee = func.attr
            else:
                continue
            if callee not in injectors:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            if first.value.startswith("mintd."):
                counts[first.value] += 1
    return dict(counts)


def _is_permitted(target: str) -> bool:
    if target.startswith(_COMPOSITION_ROOT_PREFIX) or target == _COMPOSITION_ROOT_EXACT:
        return True
    return target.endswith(_BOUNDARY_SUFFIXES)


def test_internal_monkeypatch_sites_do_not_grow() -> None:
    """Rule 1: a double stands in only for a process/network boundary or a
    composition root. Every other ``monkeypatch.setattr("mintd.…")`` is a
    grandfathered internal stub, pinned here by set equality so the set can
    only shrink.

    Both string-form mechanisms count. ``monkeypatch.setattr`` supplies 115
    of the 132 banned sites and ``unittest.mock.patch`` the other 17
    (``_fast_sync_ops._check_dvc`` 15, ``_fast_sync_ops.boto3`` 1,
    ``publish.check_project`` 1); ``patch`` is house style in
    ``test_publish.py`` / ``test_fast_sync.py`` / ``test_config.py``, so
    scanning only ``setattr`` would leave the ratchet evadable by a one-word
    change of mechanism.

    SCOPE HOLE, stated deliberately — an unstated hole is rule 4's own failure
    mode. This is a **string-form** scan. The object forms
    ``monkeypatch.setattr(obj, "attr", …)`` and ``patch.object(mod, "attr")``
    are invisible to it — 55 such ``setattr`` sites exist (``test_dvc_ops.py``
    14, ``test_cli.py`` 11, ``test_init.py`` 10, ``test_atomic.py`` 8,
    ``test_cache_ops.py`` 4, +8 elsewhere). The match is also by callee *name*,
    so ``from unittest.mock import patch as _p`` would evade it; no test in
    this suite aliases either injector today.

    Mutation that must redden this: add any
    ``monkeypatch.setattr("mintd.cli.data_pull", …)`` to any test file.
    """
    banned = {t: c for t, c in _scan_double_targets().items() if not _is_permitted(t)}

    moved = {
        t: (BANNED_TARGETS.get(t, 0), banned.get(t, 0))
        for t in set(banned) | set(BANNED_TARGETS)
        if BANNED_TARGETS.get(t) != banned.get(t)
    }
    assert banned == BANNED_TARGETS, (
        "internal monkeypatch census moved; the literal may only shrink. "
        f"target -> (pinned, scanned): {dict(sorted(moved.items()))}"
    )
    assert sum(banned.values()) == 132


def test_no_composition_root_wrapper_is_patched_wholesale() -> None:
    """``cli._resolve_clients`` is gone (DECIDED 2026-08-18, user).

    It bundled two collaborators behind one name, so a test that wanted a fake
    catalog got a fake DvcOps for free and vice versa. Its three handlers now
    name the two factories directly.

    What made the four patch sites safe to drop is the **handler set**, not —
    as an earlier draft of this docstring claimed — that each already patched
    both factories beside it. Two of the four did not: ``test_data_push.py:30``
    patched ``_resolve_dvc_ops`` but never ``_resolve_catalog_client``, and
    ``test_cli.py:3338`` patched ``_resolve_catalog_client`` but never
    ``_resolve_dvc_ops``. They are safe because ``_handle_data_push``
    (``cli.py:926``) and ``_handle_data_verify`` (``:1344``) resolve only dvc
    ops, and ``_handle_check`` (``:753``) resolves only the catalog client — so
    neither module ever reaches the factory it left unpatched. Stated wrongly,
    that precondition reads as a general licence to drop a wrapper patch on a
    handler that really does resolve both.

    Pairs with rule 1's ratchet. Rule 1 permits any ``mintd.cli._resolve_*``
    target by prefix, so this deletion could have been made to *look* free by
    relabelling it into the other bucket; instead the name is gone from
    production, from both literals, and from every patch site — a real shrink,
    -4 sites / -1 target off a census of 194 / 47. ``BANNED_TARGETS`` stays at
    132 / 28 and must not move here. The running totals live in
    ``test_the_checked_in_literal_matches_a_fresh_scan``, not here, so an
    unrelated permitted patch added in the same slice cannot masquerade as
    this deletion failing.

    Mutation that must redden this: reintroduce ``_resolve_clients`` in
    ``src/mintd/cli.py``, or patch that name from any test.
    """
    assert "_resolve_clients" not in (
        REPO_ROOT / "src" / "mintd" / "cli.py"
    ).read_text(encoding="utf-8")

    assert "mintd.cli._resolve_clients" not in _scan_double_targets()

    assert "mintd.cli._resolve_clients" not in PERMITTED_TARGETS
    assert "mintd.cli._resolve_clients" not in BANNED_TARGETS
    assert sum(BANNED_TARGETS.values()) == 132
    assert len(BANNED_TARGETS) == 28


def test_the_checked_in_literal_matches_a_fresh_scan() -> None:
    """The literals above are the *whole* census (193 sites / 47 targets), not
    a hand-copied excerpt, and the scanner is re-run here to prove it.

    This is the guard that ``test_internal_monkeypatch_sites_do_not_grow``
    cannot be: narrowing the matcher (e.g. requiring the receiver to be
    ``monkeypatch``) drops a *permitted* site, leaves the banned bucket
    untouched, and so passes rule 1 while quietly changing what rule 1
    measures. A hand-copied seed is why 96 / 110 / 115 / 140 / 158 / 163 / 164
    all appear in this document set for the same quantity.

    Mutation that must redden this: restrict ``_scan_double_targets`` to
    ``monkeypatch``-receiver calls (193 → 192), or drop ``patch`` (193 → 163).
    """
    assert set(BANNED_TARGETS) & set(PERMITTED_TARGETS) == set()

    scanned = _scan_double_targets()

    assert scanned == BANNED_TARGETS | PERMITTED_TARGETS
    assert sum(scanned.values()) == 193
    assert all(_is_permitted(t) for t in PERMITTED_TARGETS)


# ---------------------------------------------------------------------------
# Rule 4 — ratchet, don't exempt
# ---------------------------------------------------------------------------

#: module → the env vars that gate any test in it. Was a flat set of module
#: names matched by grepping for one hardcoded variable; see the docstring.
ENV_GATED_MODULES: dict[str, list[str]] = {
    "test_enclave_pull_integration.py": ["MINTD_RUN_INTEGRATION"],
    "test_producer_integration.py": ["MINTD_RUN_INTEGRATION"],
    "test_scaffold_contract.py": ["MINTD_NETWORK_TESTS"],
}


def _reads_environ(node: ast.AST) -> bool:
    """Is this expression an environment read, as opposed to any old ``.get``?

    Receiver-checked on purpose. A bare ``<anything>.get("str")`` match would
    make ``@pytest.mark.skipif(sys.platform == "win32", reason=REASONS.get("win"))``
    register ``win`` as a gating variable and redden this ratchet on an
    unrelated edit — a ratchet that fires on innocent code is one people delete.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        recv, attr = node.func.value, node.func.attr
        if attr == "getenv" and isinstance(recv, ast.Name) and recv.id == "os":
            return True
        if attr == "get":
            if isinstance(recv, ast.Attribute) and recv.attr == "environ":
                return True
            if isinstance(recv, ast.Name) and recv.id == "environ":
                return True
    if isinstance(node, ast.Subscript):
        recv = node.value
        if isinstance(recv, ast.Attribute) and recv.attr == "environ":
            return True
        if isinstance(recv, ast.Name) and recv.id == "environ":
            return True
    return False


def _env_vars_read(node: ast.AST) -> set[str]:
    """Env var names read anywhere under ``node``."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if not _reads_environ(sub):
            continue
        arg = sub.args[0] if isinstance(sub, ast.Call) and sub.args else (
            sub.slice if isinstance(sub, ast.Subscript) else None
        )
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
    return found


def _contains_skip_call(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "skip"
        for sub in ast.walk(node)
    )


def _scan_env_gated() -> dict[str, list[str]]:
    """Every module with an env-var-driven skip, by MECHANISM not by name.

    Three shapes, and the third is the one the first version of this scan
    missed. It ran ``_env_vars_read`` over the ``pytest.skip(...)`` call node,
    whose subtree holds only the message and ``allow_module_level`` — the env
    read lives in the SIBLING ``if`` test:

        if os.environ.get("MINTD_SLOW_TESTS") != "1":
            pytest.skip("set MINTD_SLOW_TESTS=1", allow_module_level=True)

    So that branch was inert: it could only fire for
    ``pytest.skip(reason=os.environ.get("WHY"))``, which is never a gate.
    Measured against an injected fourth module, **3 of 4 idioms evaded** — the
    body-level ``if``/skip, the module-level ``allow_module_level`` form, and
    the ``os.environ["X"]`` subscript variant. Only the decorator reddened.
    The repo already carries 11 ``if <cond>: pytest.skip(...)`` sites, each one
    condition-edit away from an invisible gate.
    """
    gated: dict[str, list[str]] = {}
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            # 1. `@pytest.mark.skipif(...)`, on a function or a class.
            for dec in getattr(node, "decorator_list", []):
                if "skipif" in ast.dump(dec):
                    found |= _env_vars_read(dec)
            # 2. `if <env read>: pytest.skip(...)` — the whole If, so the test
            #    expression is in scope, not just the call.
            if isinstance(node, ast.If) and _contains_skip_call(node):
                found |= _env_vars_read(node.test)
        # 3. Module-level `pytestmark = pytest.mark.skipif(...)`.
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "pytestmark" for t in node.targets
            ):
                found |= _env_vars_read(node)
        if found:
            gated[path.name] = sorted(found)
    return gated


def test_no_new_env_gated_test_modules() -> None:
    """Rule 4: a test that only runs behind an env var runs nowhere by
    default. Three modules are grandfathered; a fourth needs a reason in
    review, not a quiet decorator.

    SCOPE HOLE CLOSED (1b). This used to grep raw file text for the single
    string ``MINTD_RUN_INTEGRATION``, which made it loud in the harmless
    direction (a module merely *mentioning* the name reddened it) and blind in
    the harmful one: `tests/test_scaffold_contract.py` has gated a test on
    ``MINTD_NETWORK_TESTS`` the whole time and this ratchet could not see it.
    It now pins the MECHANISM — any env-var-driven skip — so a fourth module
    cannot escape by choosing a new variable name.

    **One correction to how the hole was filed.** The backlog says pinning
    "module-level skipif on any env var" would catch the scaffold-contract
    case. It would not: that gate is a per-FUNCTION ``@pytest.mark.skipif``
    (`test_c5_requirements_resolvable_network`), not a module-level one. The
    scan covers both, and the literal is a module → variables map rather than
    a bare set of module names, so which variable gates what is visible in the
    diff.

    Mutation that must redden this: gate any fourth module on any env var.
    """
    assert _scan_env_gated() == ENV_GATED_MODULES


PER_FILE_IGNORES = {
    "src/mintd/cli.py": ["T201"],
    "src/mintd/config_ops.py": ["T201"],
    "src/mintd/files/**": ["T201", "E501", "F401", "F811"],
    "tests/**": ["T201", "E501"],
}

#: The rest of ``[tool.ruff.lint]``. Pinned because `per-file-ignores` is only
#: the most VISIBLE way to exempt code — see the docstring below.
LINT_SELECT = ["E", "F", "T201"]
LINT_IGNORE = ["E501"]
#: per-line ``noqa`` comments under `src/`. A count, not a location list: the point is
#: that adding one is a visible edit, and pinning line numbers would redden on
#: every unrelated insertion.
SRC_NOQA_COUNT = 5


def test_ruff_per_file_ignores_shrink_only() -> None:
    """Rule 4: every lint exemption is enumerated here, so adding one is a
    visible edit. ``src/mintd/_console.py = ["T201"]`` was in this map with
    zero offenders behind it and is deleted — the ratchet's first shrink.

    SCOPE HOLE CLOSED (1b). This used to pin ``per-file-ignores`` and nothing
    else, which left three cheaper exemptions completely green — all three
    executed against the tree at S0 and confirmed to evade it:

      1. a per-line ``noqa`` comment on the offender, in ANY of the four
         spellings ruff accepts,
      2. adding ``T201`` to the global ``ignore`` list,
      3. narrowing ``select`` so the rule never fires.

    Each defeats the ratchet without touching the map it pinned. All three are
    now pinned: the whole ``[tool.ruff.lint]`` table, plus a census of ``#
    noqa`` under ``src/``. The ratchet's premise is that the cheap direction
    must be *down*; leaving three cheaper doors open made that false.

    Mutation that must redden this: add any per-file-ignore entry, add any
    code to ``ignore``, remove any code from ``select``, or add a ``noqa``
    comment anywhere under ``src/``.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint = pyproject["tool"]["ruff"]["lint"]

    assert lint["per-file-ignores"] == PER_FILE_IGNORES
    assert lint["select"] == LINT_SELECT
    assert lint["ignore"] == LINT_IGNORE
    # Nothing else may appear in the table — a key this test does not know
    # about is an exemption mechanism it is not pinning.
    assert sorted(lint) == ["ignore", "per-file-ignores", "select"]

    # Ruff accepts `#noqa`, `#  noqa`, `# NOQA` and `#NOQA` as directives — all
    # four verified rc=0 against `--select T201`. A literal `"# noqa"` count saw
    # exactly ONE spelling, so appending `print(...)  #noqa: T201` to
    # `src/mintd/_console.py` left BOTH `ruff check src` and this ratchet green.
    # `ruff format` does normalize the unspaced form, but nothing gates it: CI
    # runs only `ruff check src tests` (.github/workflows/test.yml:38, :97) and
    # there is no pre-commit config at the repo root, so a deliberate evasion
    # never passes through the formatter.
    pattern = re.compile(r"#\s*noqa", re.IGNORECASE)
    noqa = sum(
        len(pattern.findall(line))
        for path in (REPO_ROOT / "src").rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    assert noqa == SRC_NOQA_COUNT, (
        f"{noqa} noqa comments under src/, pinned at {SRC_NOQA_COUNT}. "
        "A noqa is a per-line lint exemption and is the cheapest way to "
        "escape this ratchet."
    )


# ---------------------------------------------------------------------------
# Rule 2 (census half) — every fake is licensed by a contract test
# ---------------------------------------------------------------------------

# Rule 2 is stated in `tests/test_dvc_ops_contract.py`: *a fake earns the right
# to stand in for a boundary only if some test runs unchanged over both it and
# the real thing*. That file licenses ONE fake. Nothing until now measured how
# many were unlicensed, so the rule read as satisfied when it was satisfied
# once out of seven times.
#
# What that cost: `_fakes/reporter.py` describes itself as "a deterministic,
# rich-free Reporter". Rich-free is precisely what made it unable to witness a
# rich defect -- `enclave pull`'s `[<source_path>]` subscription label was
# being DELETED by rich's markup parser, and an absolute path raised
# MarkupError out of a data-custody verb. The guarding test injected that fake
# and stayed green. Mutation testing did not catch it either: reverting the
# label reddened the test, so the mutation table called the guard pinned while
# the feature was broken in production. Only a contract test finds this class.

# Fakes with a `params=["fake", "real"]` contract test. This literal may only
# GROW -- the opposite direction from rule 1's, because here the licensed set
# is the good one.
LICENSED_FAKES = {
    "dvc_ops",     # tests/test_dvc_ops_contract.py
    "reporter",    # tests/test_reporter_contract.py
}

# Unlicensed as of 2026-08-22. Each is a fake standing in for a boundary with
# nothing proving it behaves like that boundary. Shrink this set by writing a
# contract test, never by deleting a line from it.
UNLICENSED_FAKES = {
    "archive_ops",
    "fast_sync_ops",
    "init_ops",
    "producer",
    "registry_git_ops",
}


def _fake_modules() -> set[str]:
    return {
        p.stem for p in (TESTS_DIR / "_fakes").glob("*.py")
        if p.stem != "__init__"
    }


def _contract_licensed_modules() -> set[str]:
    """Fakes named by a test file that parametrizes a fixture fake-vs-real."""
    licensed: set[str] = set()
    for path in TESTS_DIR.glob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        if not re.search(r"params\s*=\s*\[[\"']fake[\"']\s*,\s*[\"']real[\"']\]", source):
            continue
        for module in _fake_modules():
            if f"_fakes.{module}" in source:
                licensed.add(module)
    return licensed


def test_every_fake_is_accounted_for() -> None:
    """Rule 2, census half: no fake may appear without a verdict on it.

    A NEW fake in `tests/_fakes/` fails here until it is either given a
    contract test (add it to LICENSED_FAKES) or explicitly recorded as
    unlicensed (add it to UNLICENSED_FAKES) -- which puts the decision in a
    diff a reviewer reads, rather than in nobody's head.

    Mutation that must redden this: `touch tests/_fakes/anything.py`.
    """
    assert _fake_modules() == LICENSED_FAKES | UNLICENSED_FAKES, (
        "a fake is missing a verdict; give it a contract test or record it as "
        "unlicensed. fakes on disk vs accounted for: "
        f"{sorted(_fake_modules() ^ (LICENSED_FAKES | UNLICENSED_FAKES))}"
    )


def test_the_licensed_set_matches_a_fresh_scan() -> None:
    """The literals above are not self-reported: LICENSED_FAKES must equal
    what a scan for fake-vs-real parametrization actually finds.

    Guards the failure mode rule 4 exists for -- a checked-in literal that has
    drifted from the thing it claims to measure. Deleting
    `tests/test_reporter_contract.py`, or dropping its `params=["fake","real"]`
    fixture, reddens this even though LICENSED_FAKES still names `reporter`.
    """
    scanned = _contract_licensed_modules()
    assert scanned == LICENSED_FAKES, (
        "the licensed-fake literal drifted from the scan. "
        f"pinned={sorted(LICENSED_FAKES)} scanned={sorted(scanned)}"
    )
    assert not (scanned & UNLICENSED_FAKES), (
        "a fake is in both sets; move it out of UNLICENSED_FAKES: "
        f"{sorted(scanned & UNLICENSED_FAKES)}"
    )


def test_every_spinner_label_is_wrapped_in_text() -> None:
    """Every string that reaches a rich Spinner must be a `Text`, never a str.

    `rich.Status` -> `Spinner.__init__` runs `Text.from_markup(text)` for a
    `str` and IGNORES the console's `markup=False`, so a bare-str label
    silently deletes bracketed user data (a `--source-path`) and raises
    `MarkupError` -- not a `ValueError`, so it escapes every CLI handler -- on
    one starting with `/`.

    There are two kinds of site and BOTH matter: `_active_status.update(...)`
    relabels a live spinner, and `console.status(...)` CONSTRUCTS one. An
    earlier version of this ratchet scanned only `.update` and only checked
    which function the call sat in, so it stayed green while `progress()`
    rebuilt the spinner from a raw str -- a guard that did not guard, which is
    the failure this file exists to make visible. It now checks the ARGUMENT at
    every site, which is the thing that actually has to be true.

    Mutations that must redden this: drop the `Text(...)` wrapper at any of the
    three sites in `_console.py` -- `status()`, `_set_spinner()`, `progress()`.
    (Three, not four: `_set_spinner` collapsed what used to be several
    `.update` call sites into one. Re-derived, not quoted.)
    """
    source = (REPO_ROOT / "src" / "mintd" / "_console.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _is_spinner_site(call: ast.Call) -> bool:
        func = call.func
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr == "update":
            inner = func.value
            return isinstance(inner, ast.Attribute) and inner.attr == "_active_status"
        if func.attr == "status":
            inner = func.value
            return isinstance(inner, ast.Attribute) and inner.attr in {"_stderr", "_stdout"}
        return False

    bare: list[int] = []
    sites = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_spinner_site(node)):
            continue
        sites += 1
        if not node.args:
            continue
        arg = node.args[0]
        wrapped = (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "Text"
        )
        if not wrapped:
            bare.append(node.lineno)

    assert sites >= 3, (
        f"expected at least 3 spinner sites in _console.py, found {sites} -- "
        "the scan stopped matching; re-derive it rather than lowering this"
    )
    assert bare == [], (
        "these spinner labels are raw strings and will be parsed as rich markup; "
        f"wrap each in Text(): _console.py lines {bare}"
    )
