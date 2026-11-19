"""The live tier: a PR's mintd against real products, read-only, run by hand.

It checks what the stories check, on the lab's real storage: a clone lands
every tracked file and each matches its md5, a second pull downloads nothing,
an import lands the primary, a killed clone restarts and finishes, and how long
a clone takes against plain `git clone` + `dvc pull`. Run it on a lab Linux box
with a large disk before merging a PR that changes the download or push paths.
It is never a CI check, and the default `pytest` run does not collect it.

Run it only with the lab's read-only key. The tier can run clone, import and
pull only (`tests/_harness/live.py`), but that cannot stop a PR whose own clone
or pull code writes with the key it is given, so it refuses any key but the
read-only one: `AWS_SHARED_CREDENTIALS_FILE` must name a file holding a
`[mintd-live-readonly]` section and no `[mintd]` section, and `AWS_CONFIG_FILE`
one holding `[profile mintd-live-readonly]`, of which only `region` and
`endpoint_url` are used. dvc's global and system config are left out, so no
remote key set there reaches dvc. GitHub is another matter: clones use the
operator's own git credentials, which can push, so read the PR's diff before
running the tier on it.

    AWS_SHARED_CREDENTIALS_FILE=~/.aws/mintd-live-readonly-credentials \\
    AWS_CONFIG_FILE=~/.aws/mintd-live-readonly-config \\
    MINTD_BIN=<the PR's venv>/bin/mintd MINTD_LIVE_PRODUCTS=<list file> \\
    uv run pytest tests/live -rA --basetemp=<a dedicated empty folder on the large disk> [--live-k K] [-k <check>]

- `--basetemp` (required): pytest empties it at the start of every run, so
  name a folder of its own. A non-empty folder this tier did not make is
  refused. A failed check keeps its folder, which may hold licensed data:
  delete `--basetemp` after the run.  The copy of the key is deleted at the end
  of the session.
- Disk: about twice the largest product at peak (the shared clone, one more
  copy per check, the import's primary), more if dvc copies rather than links.
  `-k <check>` runs one check at a time. Run serially, never with `-n`.
- Products: the list file holds one catalog name per line; a line starting
  with `#` is a comment. Without `MINTD_LIVE_PRODUCTS`, three public products
  run. Never put a non-public name in the repo. Test ids, the `live results`
  summary and `.pytest_cache` name every listed product, so remove non-public
  names before pasting output into a PR or issue.
- Config: the box's own mintd config (`$MINTD_CONFIG_DIR` or
  `~/.config/mintd/config.yaml`), read, never written: mintd runs on a copy with
  its own `cache_dir`. The tier fails, never skips, without it or the key.
- Results: counts, bytes, md5 matches and times for public and labonly
  products; counts only for any other or missing classification, failure
  messages included. Warnings are not redacted.
- `--live-k K`: fail when mintd's clone takes more than K times plain
  `git clone` + `dvc pull`. Without it the ratio is reported only.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
import yaml

from tests._harness import HarnessError
from tests._harness.consumer import fresh_project  # noqa: F401
from tests._harness.live import (
    MARKER, RESULTS, SEEN, LiveMintd, Product, catalog_entry, check_basetemp, readonly_credentials, redact,
    run_read_only,
)
from tests._harness.mintd import mintd_bin  # noqa: F401

# Read at import, before any fixture runs: tests/conftest.py's autouse fixtures
# later point AWS_SHARED_CREDENTIALS_FILE at a missing file and move dvc's dirs.
_OPERATOR_ENV = dict(os.environ)
_DROPPED = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"}


def _products() -> list[str]:
    listed = _OPERATOR_ENV.get("MINTD_LIVE_PRODUCTS")
    if not listed:
        return ["cms-price-transparency-registry", "irs-eo-bmf", "misc-geography"]
    names = [ln.strip() for ln in Path(listed).read_text(encoding="utf-8").splitlines()]
    names = [n for n in names if n and not n.startswith("#")]
    if not names:
        raise ValueError(f"{listed} lists no product")
    return names


PRODUCTS = _products()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live-k", type=float, default=None,
                     help="fail when mintd's clone takes more than K times plain git clone + dvc pull")


def pytest_configure(config: pytest.Config) -> None:
    # A long traceback prints each frame's arguments, and -l its locals: the
    # operator's environment among them, with any key or token it holds.
    config.option.showlocals = config.option.fulltrace = False
    if config.option.tbstyle in ("auto", "long"):
        config.option.tbstyle = "short"
    if config.option.help or config.option.collectonly:
        return
    try:
        check_basetemp(config.option.basetemp)
    except HarnessError as e:  # a usage error, before anything runs
        raise pytest.UsageError(str(e)) from None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> pytest.TestReport:
    report = yield
    redact(item, call, report, SEEN)
    return report


def pytest_terminal_summary(terminalreporter) -> None:
    if RESULTS:
        terminalreporter.section("live results")
        for line in RESULTS:
            terminalreporter.write_line(line)


@pytest.fixture(scope="session")
def live(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> LiveMintd:
    """The PR's mintd, with the box's config and the read-only key."""
    config_dir = _OPERATOR_ENV.get("MINTD_CONFIG_DIR")
    config = Path(config_dir) / "config.yaml" if config_dir else Path(_OPERATOR_ENV["HOME"]) / ".config/mintd/config.yaml"
    if not config.is_file():
        pytest.fail(f"no mintd config at {config}: the live tier runs with the box's own config")
    settings = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    if not settings.get("registry_url"):
        pytest.fail(f"{config} names no registry_url")
    work = tmp_path_factory.getbasetemp()  # pytest empties a given --basetemp here
    (work / MARKER).touch()
    credentials = readonly_credentials(_OPERATOR_ENV, work / "aws")
    request.addfinalizer(lambda: credentials.unlink(missing_ok=True))
    (work / "mintd-config").mkdir(exist_ok=True)
    copy = work / "mintd-config" / "config.yaml"
    copy.write_text(yaml.safe_dump({**settings, "cache_dir": str(work / "mintd-cache")}), encoding="utf-8")
    bin_ = request.getfixturevalue("mintd_bin")
    env = {k: v for k, v in _OPERATOR_ENV.items() if k not in _DROPPED}
    env.update({
        "PATH": f"{bin_.parent}{os.pathsep}{_OPERATOR_ENV['PATH']}",  # the wheel's dvc, for mintd and plain runs
        "TMPDIR": str(work),
        "MINTD_CONFIG_DIR": str(copy.parent),
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
        "AWS_CONFIG_FILE": str(credentials.with_name("config")),  # the read-only profile's region and endpoint only
        "GIT_TERMINAL_PROMPT": "0",
        # Empty, so no remote key in the box's dvc config can stand in for the read-only one.
        "DVC_GLOBAL_CONFIG_DIR": str(work / "dvc-global"),
        "DVC_SYSTEM_CONFIG_DIR": str(work / "dvc-system"),
        "DVC_SITE_CACHE_DIR": str(work / "dvc-site"),
        "DVC_NO_ANALYTICS": "1",
    })
    mintd = LiveMintd(bin_, env, copy, timeout=None)
    RESULTS.append(f"mintd under test: {mintd('--version', cwd=work, json=False).stdout.strip()}")
    return mintd


@pytest.fixture(scope="session")
def catalog(live: LiveMintd, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The registry, cloned once, for each product's classification and URL."""
    registry = yaml.safe_load(live.config.read_text(encoding="utf-8"))["registry_url"]
    dest = tmp_path_factory.mktemp("registry") / "registry"
    got = run_read_only(["git", "clone", "-q", "--depth", "1", registry, str(dest)], env=live.env)
    if got.returncode:
        pytest.fail(f"cloning the registry failed: {got.stderr}")
    return dest


@pytest.fixture(scope="module", params=PRODUCTS)
def product(request: pytest.FixtureRequest, catalog: Path) -> Product:
    entry = catalog_entry(catalog, request.param)
    if entry is None:
        pytest.fail(f"no catalog entry for {request.param!r}")  # names only what the test id already shows
    SEEN[request.param] = Product(request.param, None, None)  # counts only until its classification is read
    meta = yaml.safe_load(entry.read_text(encoding="utf-8")) or {}
    found = Product(
        request.param, (meta.get("governance") or {}).get("classification"),
        (meta.get("repository") or {}).get("github_url"),
    )
    SEEN[found.name] = found
    return found


@pytest.fixture(scope="module")
def cloned(product: Product, live: LiveMintd, tmp_path_factory: pytest.TempPathFactory):
    """One `mintd data clone` of the product, shared by the checks that read it."""
    dest = tmp_path_factory.mktemp("clone") / "product"
    started = time.monotonic()
    run = live("data", "clone", product.name, "--dest", str(dest), cwd=dest.parent)
    seconds = time.monotonic() - started
    assert run.returncode == 0, run.stderr
    yield dest, seconds
    shutil.rmtree(dest.parent, ignore_errors=True)
