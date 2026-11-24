"""The live tier's rules: what it may run, which key it runs with, and what a
report may show. Also story 11's rules for its run on the lab's test bucket:
which bucket, and which key. Both tiers run a key copy in the same locked-down
environment. Kept apart from `tests/live` so the unit suite can test them
without real storage.
"""

from __future__ import annotations

import configparser
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
#: Story 11's opt-in variable, which names the lab's test bucket at run time, and that bucket's key's profile.
BUCKET_VAR, TEST_PROFILE = "MINTD_STORY_BUCKET", "mintd-test"
#: The SHA-256 of the test bucket's name: the one bucket a story may write to. The operator names it in
#: MINTD_STORY_BUCKET, and any other name is refused. The hash keeps the name out of casual reading of this public
#: repo. It doesn't keep the name from someone who guesses it: checking a guess takes one hash.
TEST_BUCKET_SHA256 = "328fdfb5a7427fc030248d85272bfd45528013dc716282d7bb53a273b3f95e92"
#: The test bucket's endpoint: Wasabi's service host, or its regional form, over https. botocore addresses a custom
#: endpoint path-style, so a host with a bucket label in front (`other.s3.wasabisys.com`) would send every request,
#: writes included, to that other bucket. Only the bare service host is accepted.
TEST_ENDPOINT_SCHEME = "https"
TEST_ENDPOINT_HOST = re.compile(r"s3(\.[a-z]{2}-[a-z]+-[0-9]+)?\.wasabisys\.com")
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


def _named_file(operator_env: Mapping[str, str], var: str, profile: str) -> tuple[Path, configparser.ConfigParser]:
    """The AWS file `var` names, parsed. A refusal names the file, never what is in it."""
    named = operator_env.get(var)
    if not named:
        raise HarnessError(f"this run uses only the {profile} key: set {var} to its file, the one holding the "
                           f"{profile} profile")
    src = Path(named).expanduser()
    defaults = [Path.home() / ".aws" / n for n in ("credentials", "config")]
    # The same file, not the same path: a link to it, or another case on a case-insensitive disk, is refused too.
    if src.is_file() and any(d.is_file() and src.samefile(d) for d in defaults):
        raise HarnessError(f"{src} ({var}) is a default AWS file, which can hold any key: name a file of its own, "
                           f"holding only the {profile} profile")
    # No default section: a [DEFAULT] is a section like any other, so no key falls through from it.
    found = configparser.ConfigParser(interpolation=None, default_section="\n")
    try:
        read = found.read(src, encoding="utf-8")
    except configparser.Error as e:  # its message can quote a line, so only its type is shown
        raise HarnessError(f"{src} ({var}) does not parse ({type(e).__name__})") from None
    if not read:
        raise HarnessError(f"no file at {src} ({var})")
    return src, found


def readonly_credentials(operator_env: Mapping[str, str], dest: Path, profile: str = READONLY_PROFILE) -> Path:
    """The key of `profile` and its settings, where mintd and dvc read them: by
    default the live tier's read-only key; story 11's bucket run passes
    `TEST_PROFILE`.

    Only from the two files the operator names, never `~/.aws` (named by path,
    it is refused): `AWS_SHARED_CREDENTIALS_FILE` must hold a `[<profile>]`
    section with both halves of a key, no `[mintd]` one (the lab's key, which
    can write) and no `[DEFAULT]`, and `AWS_CONFIG_FILE` a `[profile <profile>]`
    section. The key's
    two halves, and nothing else from the section, are copied under `[mintd]`,
    the profile mintd looks for and product remotes name, to `dest/credentials`
    (mode 0600); the profile's `region` and `endpoint_url`, and nothing else,
    under `[profile mintd]` to `dest/config`. Returns the credentials copy.
    """
    src, found = _named_file(operator_env, "AWS_SHARED_CREDENTIALS_FILE", profile)
    if found.has_section("mintd"):
        raise HarnessError(f"{src} holds [mintd], the lab's key, which can write; name a file with only [{profile}]")
    if found.has_section("DEFAULT"):  # botocore lets every profile inherit its keys
        raise HarnessError(f"{src} has a [DEFAULT] section, whose keys every profile inherits; name a file with only "
                           f"[{profile}]")
    if not found.has_section(profile):
        raise HarnessError(f"{src} has no [{profile}] section")
    key = {k: found[profile].get(k, "").strip() for k in ("aws_access_key_id", "aws_secret_access_key")}
    if not all(key.values()):  # else boto would look for a key further along its chain
        raise HarnessError(f"{src}'s [{profile}] section lacks aws_access_key_id or aws_secret_access_key")
    conf, settings = _named_file(operator_env, "AWS_CONFIG_FILE", profile)
    section = f"profile {profile}"
    if not settings.has_section(section):
        raise HarnessError(f"{conf} has no [{section}] section")
    copy = configparser.ConfigParser(interpolation=None)
    copy["mintd"] = key
    config = configparser.ConfigParser(interpolation=None)
    config["profile mintd"] = {k: v for k, v in settings[section].items() if k in ("region", "endpoint_url")}
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "credentials"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        copy.write(fh)
    with (dest / "config").open("w", encoding="utf-8") as fh:
        config.write(fh)
    return path


def story_bucket(named: str | None) -> str | None:
    """The bucket story 11 writes to: None for the emulator, else only the lab's
    test bucket, the name whose SHA-256 is `TEST_BUCKET_SHA256`. Takes the
    variable's value, so no environment sits in its frame."""
    if not named:
        return None
    if hashlib.sha256(named.encode("utf-8")).hexdigest() != TEST_BUCKET_SHA256:
        raise HarnessError(f"{BUCKET_VAR}={named!r} isn't the lab's test bucket, the one bucket a story writes to")
    return named


def profile_endpoint(credentials: Path) -> str:
    """The `endpoint_url` of the `[profile mintd]` copy beside `credentials`, a
    file the harness wrote. Refused when missing, so no request can fall back to
    AWS's own endpoint; and unless it is the test bucket's endpoint host alone
    (`TEST_ENDPOINT_SCHEME`, `TEST_ENDPOINT_HOST`), so no request can land in
    another bucket, whether named by a path or by a label in front of the host."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(credentials.with_name("config"), encoding="utf-8")
    endpoint = config.get("profile mintd", "endpoint_url", fallback="").strip()
    if not endpoint:
        raise HarnessError(f"[profile {TEST_PROFILE}] in AWS_CONFIG_FILE has no endpoint_url: name the endpoint, or "
                           "every request would go to AWS's own")
    parts = urlsplit(endpoint)
    if parts.path.strip("/") or parts.query or parts.fragment:  # path-style requests would go to the bucket it names
        raise HarnessError(f"[profile {TEST_PROFILE}]'s endpoint_url in AWS_CONFIG_FILE has a path: requests would land "
                           "in the bucket it names, not the test bucket; name the endpoint's host alone")
    if (parts.scheme != TEST_ENDPOINT_SCHEME or parts.username is not None or parts.password is not None
            or TEST_ENDPOINT_HOST.fullmatch(parts.hostname or "") is None):
        raise HarnessError(f"[profile {TEST_PROFILE}]'s endpoint_url in AWS_CONFIG_FILE isn't the test bucket's "
                           f"endpoint ({TEST_ENDPOINT_SCHEME}://{TEST_ENDPOINT_HOST.pattern}): a bucket label in front "
                           "of the host, or another host, would send the story's writes to another bucket")
    return endpoint


def require_no_version_listing(client: Any, bucket: str, prefix: str) -> None:
    """Refuse a key that may list versions under `prefix`, before anything is
    written. dvc's push lists an object's versions before it uploads it again,
    and when it may, it sends DeleteObjects for the key first: on a versioned
    bucket, a delete marker. A key refused ListObjectVersions gets no further,
    so the story deletes nothing only with such a key."""
    from botocore.exceptions import ClientError

    try:
        client.list_object_versions(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "AccessDenied":
            return
        raise HarnessError(f"could not check that the key is refused ListObjectVersions under {prefix} ({code})") from None
    raise HarnessError(f"the key may list versions under {prefix}, so mintd's push would send DeleteObjects for the "
                       "object it uploads again, a delete marker on a versioned bucket: a story writes only with a key "
                       "refused ListObjectVersions")


def kept(name: str) -> bool:
    """Whether a variable may sit beside a key copy: no AWS, boto or fsspec
    setting but the trusted-certificates bundle, so no key but the copy is in
    reach (fsspec's S3 defaults override a profile's key)."""
    return name == "AWS_CA_BUNDLE" or not name.startswith(("AWS_", "BOTO_", "FSSPEC_"))


def lock_down(env: Mapping[str, str], credentials: Path, work: Path) -> dict[str, str]:
    """`env` with the key copy at `credentials` (from `readonly_credentials`)
    as the only key in reach: every other AWS, boto and fsspec variable dropped
    (`AWS_CA_BUNDLE` stays: it picks trusted certificates, not a key),
    `BOTO_CONFIG` an empty file, the EC2 metadata service off, no fsspec config
    folder, and dvc's global and system config empty, so no remote key set
    there reaches dvc."""
    (work / "boto").touch()
    return {k: v for k, v in env.items() if kept(k)} | {
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
        "AWS_CONFIG_FILE": str(credentials.with_name("config")),  # the profile's region and endpoint only
        "BOTO_CONFIG": str(work / "boto"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "FSSPEC_CONFIG_DIR": str(work / "fsspec"),  # no folder there: no ~/.config/fsspec key
        "DVC_GLOBAL_CONFIG_DIR": str(work / "dvc-global"),
        "DVC_SYSTEM_CONFIG_DIR": str(work / "dvc-system"),
    }


def quiet_tracebacks(config: Any) -> None:
    """A run that holds a real key prints no frame's arguments or locals, and
    no botocore request log."""
    # A long traceback prints each frame's arguments, and -l its locals: the
    # operator's environment among them, with any key or token it holds.
    config.option.showlocals = config.option.fulltrace = False
    if config.option.tbstyle in ("auto", "long"):
        config.option.tbstyle = "short"
    # botocore's DEBUG log prints each request's headers, the key id in Authorization among them, and pytest's
    # --log-level=DEBUG would capture and print it.
    logging.getLogger("botocore").setLevel(logging.WARNING)


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
