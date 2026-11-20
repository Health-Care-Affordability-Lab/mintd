"""The live tier's rules: what it may run, which key it runs with, and what a
report may show. Kept apart from `tests/live` so the unit suite can test them
without real storage.
"""

from __future__ import annotations

import configparser
import contextlib
import dataclasses
import json
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tests._harness import HarnessError
from tests._harness.expect import Mismatch
from tests._harness.mintd import Mintd, Run

#: The only commands the live tier runs: it reads real products, never writes.
READ_ONLY = (
    ("mintd", "data", "clone"), ("mintd", "data", "import"), ("mintd", "data", "pull"), ("mintd", "--version"),
    ("git", "clone"), ("dvc", "pull"),
)
#: The profile of the lab's read-only key, in the files AWS_SHARED_CREDENTIALS_FILE and AWS_CONFIG_FILE name.
READONLY_PROFILE = "mintd-live-readonly"
#: Classifications whose paths, md5s, bytes and times a report may show.
DETAILED = frozenset({"public", "labonly"})
#: The file the live tier leaves in its --basetemp, so a later run knows the folder is its own.
MARKER = ".mintd-live"
#: Catalog type folders in the order mintd searches them (`_catalog_cache._TYPE_DIRS`).
CATALOG_TYPES = ("data", "code", "project", "enclave")


def check_basetemp(given: str | None) -> None:
    """pytest empties `--basetemp` at the start of a run, so the live tier
    needs one (a product must not land in the system temp folder) and refuses
    a folder holding files it did not make."""
    if not given:
        raise HarnessError("the live tier needs --basetemp: a dedicated empty folder on a disk large enough for the products")
    folder = Path(given)
    if folder.is_dir() and any(folder.iterdir()) and not (folder / MARKER).exists():
        raise HarnessError(f"--basetemp {given} holds files this tier did not make, and pytest empties it at the start of "
                           "a run: name a dedicated empty folder")


def catalog_entry(catalog: Path, name: str) -> Path | None:
    """The entry mintd reads for `name`: the first type folder holding it."""
    return next((p for t in CATALOG_TYPES if (p := catalog / "catalog" / t / f"{name}.yaml").is_file()), None)


def read_only(argv: Sequence[str]) -> None:
    """Refuse any command that is not on `READ_ONLY`, before it is spawned."""
    if not any(tuple(argv[: len(allowed)]) == allowed for allowed in READ_ONLY):
        raise HarnessError(
            f"the live tier is read-only and refuses {' '.join(argv[:3])!r}; it runs only "
            + ", ".join(" ".join(allowed) for allowed in READ_ONLY)
        )


#: Every mintd, git and plain dvc the live tier started, each in a session of
#: its own, so a SIGTERM or SIGHUP can kill the groups still running.
STARTED: list[subprocess.Popen[str]] = []


def run_read_only(argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """git or plain dvc, through the same allowlist. Like mintd it runs in a
    session of its own, which the signal handler can kill whole; a Ctrl-C
    kills its group here."""
    read_only(argv)
    proc = subprocess.Popen(
        list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    STARTED.append(proc)
    try:
        out, err = proc.communicate()
    except BaseException:  # a Ctrl-C, which its own session never sees: nothing it started outlives the call
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


class LiveMintd(Mintd):
    """`Mintd` that refuses a command outside `READ_ONLY`. A call goes through
    `start`, so this one check covers both."""

    def start(self, *args: str, cwd: Path, json: bool = True) -> Run:
        read_only(("mintd", *args))
        run = super().start(*args, cwd=cwd, json=json)
        STARTED.append(run.proc)
        return run


def _named_file(operator_env: Mapping[str, str], var: str) -> tuple[Path, configparser.ConfigParser]:
    """The AWS file `var` names, parsed. A refusal names the file, never what is in it."""
    named = operator_env.get(var)
    if not named:
        raise HarnessError(f"the live tier runs only with the lab's read-only key: set {var} to its file, the one "
                           f"holding the {READONLY_PROFILE} profile")
    src = Path(named).expanduser()
    # No default section: a [DEFAULT] is a section like any other, so no key falls through from it.
    found = configparser.ConfigParser(interpolation=None, default_section="\n")
    try:
        read = found.read(src, encoding="utf-8")
    except configparser.Error as e:  # its message can quote a line, so only its type is shown
        raise HarnessError(f"{src} ({var}) does not parse ({type(e).__name__})") from None
    if not read:
        raise HarnessError(f"no file at {src} ({var})")
    return src, found


def readonly_credentials(operator_env: Mapping[str, str], dest: Path) -> Path:
    """The read-only key and its profile, where the live tier's mintd and dvc read them.

    Only from the two files the operator names, never `~/.aws`:
    `AWS_SHARED_CREDENTIALS_FILE` must hold a `[mintd-live-readonly]` section with
    both halves of a key and no `[mintd]` one (the lab's key, which can write),
    and `AWS_CONFIG_FILE` a `[profile mintd-live-readonly]` section. The key's
    two halves, and nothing else from the section, are copied under `[mintd]`,
    the profile mintd looks for and product remotes name, to `dest/credentials`
    (mode 0600); the profile's `region` and `endpoint_url`, and nothing else,
    under `[profile mintd]` to `dest/config`. Returns the credentials copy.
    """
    src, found = _named_file(operator_env, "AWS_SHARED_CREDENTIALS_FILE")
    if found.has_section("mintd"):
        raise HarnessError(f"{src} holds [mintd], the lab's key, which can write; give the live tier a file with only "
                           f"[{READONLY_PROFILE}]")
    if found.has_section("DEFAULT"):  # botocore lets every profile inherit its keys
        raise HarnessError(f"{src} has a [DEFAULT] section, whose keys every profile inherits; give the live tier a "
                           f"file with only [{READONLY_PROFILE}]")
    if not found.has_section(READONLY_PROFILE):
        raise HarnessError(f"{src} has no [{READONLY_PROFILE}] section")
    key = {k: found[READONLY_PROFILE].get(k, "").strip() for k in ("aws_access_key_id", "aws_secret_access_key")}
    if not all(key.values()):  # else boto would look for a key further along its chain
        raise HarnessError(f"{src}'s [{READONLY_PROFILE}] section lacks aws_access_key_id or aws_secret_access_key")
    conf, settings = _named_file(operator_env, "AWS_CONFIG_FILE")
    section = f"profile {READONLY_PROFILE}"
    if not settings.has_section(section):
        raise HarnessError(f"{conf} has no [{section}] section")
    copy = configparser.ConfigParser(interpolation=None)
    copy["mintd"] = key
    profile = configparser.ConfigParser(interpolation=None)
    profile["profile mintd"] = {k: v for k, v in settings[section].items() if k in ("region", "endpoint_url")}
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "credentials"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        copy.write(fh)
    with (dest / "config").open("w", encoding="utf-8") as fh:
        profile.write(fh)
    return path


@dataclasses.dataclass(frozen=True)
class Product:
    name: str
    classification: str | None
    url: str | None

    @property
    def shown(self) -> bool:
        """Public and labonly products show details; any other or missing
        classification shows counts only."""
        return self.classification in DETAILED

    def stricter(self, repo: Path) -> Product:
        """This product with the stricter of its catalog classification and the
        one in `repo`'s metadata.json, when `repo` has one (none there counts as
        missing, so counts only)."""
        meta = repo / "metadata.json"
        if not meta.is_file():
            return self
        theirs = (json.loads(meta.read_text(encoding="utf-8")).get("governance") or {}).get("classification")
        rank = {"public": 0, "labonly": 1}
        return self if rank.get(self.classification, 2) >= rank.get(theirs, 2) else dataclasses.replace(
            self, classification=theirs)

    def line(self, check: str, counts: str, details: str = "") -> str:
        return f"{self.name} {check}: {counts}" + (f"; {details}" if details and self.shown else "")


#: Products whose catalog entry the tier has read, by name.
SEEN: dict[str, Product] = {}
#: One line per check, for the `live results` summary.
RESULTS: list[str] = []


def redact(item: Any, call: Any, report: Any, products: Mapping[str, Product]) -> None:
    """The live tier's report rule, for each setup, call and teardown report.

    A product that is not public or labonly (or has no classification) shows
    counts only: captured output is dropped, and a failure says the exception's
    type and, for a landed-file mismatch, its counts, never a path, md5, byte
    count or time. A product not yet in `products` failed before its catalog
    entry was read (the wheel build, the config check, the registry clone),
    which touches none of its data, so that report is shown as it is.
    """
    params = getattr(getattr(item, "callspec", None), "params", {})
    product = products.get(params.get("product"))
    if product is None or product.shown:
        return
    report.sections.clear()
    if report.failed:
        exc = call.excinfo.value if call.excinfo else None
        counts = f" ({exc.counts})" if isinstance(exc, Mismatch) else ""
        report.longrepr = (
            f"{product.name}: {type(exc).__name__}{counts}; details withheld "
            f"({product.classification or 'no classification'})"
        )
