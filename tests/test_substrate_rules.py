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
            if callee not in _DOUBLE_INJECTORS:
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

ENV_GATED_MODULES = {
    "test_producer_integration.py",
    "test_enclave_pull_integration.py",
}


def test_no_new_env_gated_test_modules() -> None:
    """Rule 4: a test that only runs behind ``MINTD_RUN_INTEGRATION=1`` runs
    nowhere by default. Two modules are grandfathered; a third needs a reason
    in review, not a quiet import.

    SCOPE HOLE, stated deliberately: this greps raw file text for one
    variable name. It is loud in the harmless direction — a module that merely
    mentions ``MINTD_RUN_INTEGRATION`` in a comment reddens it — and blind in
    the harmful one: a module gated on any *other* variable, marker, or
    conftest option is invisible. ``tests/test_scaffold_contract.py:188``
    already gates on ``MINTD_NETWORK_TESTS`` and this ratchet does not see it.
    Pinning the mechanism rather than the name would catch that and changes
    the literal this slice was asked to seed, so it is a scope change this
    slice does not own.

    Mutation that must redden this: gate a third module on the same variable.
    """
    needle = "MINTD_RUN_INTEGRATION"
    gated = {
        path.name
        for path in TESTS_DIR.rglob("*.py")
        # This module names the variable in order to scan for it.
        if path.name != Path(__file__).name
        and needle in path.read_text(encoding="utf-8")
    }

    assert gated == ENV_GATED_MODULES


PER_FILE_IGNORES = {
    "src/mintd/cli.py": ["T201"],
    "src/mintd/config_ops.py": ["T201"],
    "src/mintd/files/**": ["T201", "E501", "F401", "F811"],
    "tests/**": ["T201", "E501"],
}


def test_ruff_per_file_ignores_shrink_only() -> None:
    """Rule 4: every ``per-file-ignores`` entry is enumerated here, so adding
    one is a visible edit. ``src/mintd/_console.py = ["T201"]`` was in this map
    with zero offenders behind it and is deleted — the ratchet's first shrink.

    SCOPE HOLE, stated deliberately: this pins ``per-file-ignores`` and
    nothing else. Three cheaper exemptions stay green — a ``# noqa: T201``
    comment, adding ``T201`` to the global ``ignore`` list, and narrowing
    ``select``. Pinning the whole ``[tool.ruff.lint]`` table would close them
    and is a scope change this slice does not own.

    Mutation that must redden this: add any entry to
    ``[tool.ruff.lint.per-file-ignores]``.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    configured = pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]

    assert configured == PER_FILE_IGNORES
