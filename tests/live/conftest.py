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
`endpoint_url` are used. Every other `AWS_*`, `BOTO_*` and `FSSPEC_*`
variable is dropped, `AWS_ENDPOINT_URL` too, and the other key sources are shut
(`BOTO_CONFIG` names an empty file, the EC2 metadata service is off, fsspec's
config folder does not exist), so no other key can stand in for the copy. The one exception is
`AWS_CA_BUNDLE`, which changes which TLS certificates are trusted, not which
key is used: a network that intercepts TLS needs it. dvc's global and system
config are left out, so no remote key set there reaches dvc. GitHub is another matter: clones use the
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
  names before pasting output into a PR or issue. `--durations` and a JUnit
  report give each check's time for every product, hidden ones too.
- Config: the box's own mintd config (`$MINTD_CONFIG_DIR` or
  `~/.config/mintd/config.yaml`), read, never written: mintd runs on a copy with
  its own `cache_dir`. The tier fails, never skips, without it or the key.
- Results: counts, bytes, md5 matches and times for public and labonly
  products; counts only for any other or missing classification, failure
  messages included. A product is shown by the stricter of its catalog entry
  and its cloned repo's metadata.json. Warnings are not redacted.
- A product too small to interrupt skips the killed-clone check, saying which:
  mintd finished, with every file landed, before any object was seen; or every
  object had landed when it stopped (with how it stopped). A clone that
  finishes having landed nothing fails.
- SIGTERM or SIGHUP kills every mintd, git and plain dvc still running and
  deletes the key copy. Under `nohup`, SIGHUP stays ignored and cleanup
  happens on SIGTERM.
- `--live-k K`: fail when mintd's clone takes more than K times plain
  `git clone` + `dvc pull`. Without it the ratio is reported only.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import time
from pathlib import Path

import pytest
import yaml

from tests._harness import HarnessError
from tests._harness.consumer import fresh_project  # noqa: F401
from tests._harness.live import (
    MARKER, RESULTS, SEEN, STARTED, LiveMintd, Product, catalog_entry, check_basetemp, readonly_credentials,
    redact, run_read_only,
)
from tests._harness.mintd import mintd_bin  # noqa: F401

# Read at import, before any fixture runs: tests/conftest.py's autouse fixtures
# later point AWS_SHARED_CREDENTIALS_FILE at a missing file and move dvc's dirs.
_OPERATOR_ENV = dict(os.environ)


def _kept(name: str) -> bool:
    """Whether an operator variable reaches mintd: no AWS, boto or fsspec
    setting but the trusted-certificates bundle, so no key but the copy is in
    reach (fsspec's S3 defaults override a profile's key)."""
    return name == "AWS_CA_BUNDLE" or not name.startswith(("AWS_", "BOTO_", "FSSPEC_"))


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
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}

    def stop(signum: int, _frame: object) -> None:
        """mintd, git and plain dvc each run in a session of their own, which a
        SIGTERM to pytest or a closed terminal never reaches: kill each group
        still running, delete the key copy, then die of the signal as pytest
        would have. Not installed for a signal the operator ignores: under
        nohup, SIGHUP stays ignored and cleanup happens on SIGTERM."""
        for proc in STARTED:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
        credentials.unlink(missing_ok=True)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig, handler in previous.items():
        if handler is not signal.SIG_IGN:  # under nohup a hangup stays ignored, and the run goes on
            signal.signal(sig, stop)
    request.addfinalizer(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])
    (work / "boto").touch()  # BOTO_CONFIG: an empty file, so no ~/.boto key
    (work / "mintd-config").mkdir(exist_ok=True)
    copy = work / "mintd-config" / "config.yaml"
    copy.write_text(yaml.safe_dump({**settings, "cache_dir": str(work / "mintd-cache")}), encoding="utf-8")
    bin_ = request.getfixturevalue("mintd_bin")
    env = {k: v for k, v in _OPERATOR_ENV.items() if _kept(k)}
    env.update({
        "PATH": f"{bin_.parent}{os.pathsep}{_OPERATOR_ENV['PATH']}",  # the wheel's dvc, for mintd and plain runs
        "TMPDIR": str(work),
        "MINTD_CONFIG_DIR": str(copy.parent),
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
        "AWS_CONFIG_FILE": str(credentials.with_name("config")),  # the read-only profile's region and endpoint only
        "BOTO_CONFIG": str(work / "boto"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "FSSPEC_CONFIG_DIR": str(work / "fsspec"),  # no folder there: no ~/.config/fsspec key
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
    SEEN[product.name] = SEEN[product.name].stricter(dest)  # before any check reads what landed
    assert run.returncode == 0, run.stderr
    yield dest, seconds
    shutil.rmtree(dest.parent, ignore_errors=True)
