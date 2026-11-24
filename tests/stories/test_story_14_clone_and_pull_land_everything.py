"""Story 14: Clone and pull land everything through a storage hiccup or a stale lock entry.

- Given: a published product, cloned by `mintd data clone`, or by `git clone`
  then `mintd data pull`.
- Storage hiccup case: S3 answers one GET of one file with a 503 whose body
  botocore can't parse, so it raises `'StreamingChecksumBody' object has no
  attribute 'strip'` instead of the 5xx, as Wasabi did on a real clone.
  - Then: the file is fetched again and lands; exit 0.
  - Pass: every file lands and matches its pointer, and each downloads once.
- Stale lock entry case: the producer's foreach stage shrank from `[p, q]` to
  `[p]`, and `dvc.lock` still holds `fan@q`, which dvc.yaml no longer
  declares. Plain `dvc pull` skips it; `dvc checkout` refuses any argv naming
  its output.
  - Then: everything else lands, `fan@q` is named in one warning; exit 0.
- Failed checkout case: the product also imports a folder from another
  producer, and one stage's output was renamed in dvc.yaml without a rerun,
  so `dvc checkout` refuses the lock's old name.
  - Then: the import and everything else land, the refused output is named,
    and the exit is non-zero.
- Catches: one mislabelled 5xx failing a file outright, one stale lock entry
  failing the whole clone with exit 255, and a refused checkout stopping the
  imports from landing (all found on a real clone).
"""

from __future__ import annotations

import collections
import subprocess
import sys
from pathlib import Path

import pytest

from tests._harness.expect import md5, s3_state
from tests._harness.git import _git
from tests._harness.producer import build_local_producer
from tests._harness.s3 import BUCKET

FAN = """stages:
  fan:
    foreach: [p, q]
    do:
      cmd: echo ${item} > data/fan_${item}.csv
      outs:
        - data/fan_${item}.csv
"""


def _landed(dest: Path) -> dict[str, str]:
    """`{path: md5}` of every data file on disk, pointers and dvc's .gitignore left out."""
    return {
        p.relative_to(dest).as_posix(): md5(p.read_bytes()) for p in (dest / "data").rglob("*")
        if p.is_file() and p.suffix != ".dvc" and p.name != ".gitignore"
    }


def _clone(mintd, product, tmp_path: Path, *, json: bool = True):
    dest = tmp_path / "clone"
    return dest, mintd("data", "clone", product.name, "--dest", str(dest), cwd=tmp_path, json=json)


def _git_clone_then_pull(mintd, product, tmp_path: Path, *, json: bool = True):
    dest = tmp_path / "teammate"
    _git(["clone", product.producer.url, str(dest)])
    return dest, mintd("data", "pull", cwd=dest, json=json)


@pytest.mark.parametrize("fetch", [_clone, _git_clone_then_pull], ids=["clone", "pull"])
def test_a_storage_hiccup_on_one_file_is_fetched_again(product, mintd, s3_server, tmp_path, fetch) -> None:
    hiccup = f"/{BUCKET}/{product.prefix}/data/final/a.csv"
    s3_server.fail_gets[hiccup] = (503, 1)

    dest, run = fetch(mintd, product, tmp_path)

    assert run.returncode == 0, run.stderr
    assert s3_server.failed_gets == [hiccup], "the hiccup was served"
    assert _landed(dest) == {k: md5(b) for k, b in product.published().items()}
    assert s3_state(s3_server, product.prefix).gets == collections.Counter(list(product.published())), "each file once"


def _shrink_fan(product) -> None:
    """Publish the foreach stage `fan` over `[p, q]`, then shrink it to `[p]`
    without running it again, so `dvc.lock` keeps `fan@q`."""
    producer = product.producer
    producer.publish_pipeline(FAN)
    yaml_text = (producer.work / "dvc.yaml").read_text()
    (producer.work / "dvc.yaml").write_text(yaml_text.replace("foreach: [p, q]", "foreach: [p]"))
    assert "fan@q:" in (producer.work / "dvc.lock").read_text()
    _git(["commit", "-q", "-am", "fan shrinks"], cwd=producer.work)
    _git(["push", "-q"], cwd=producer.work)


@pytest.mark.parametrize("fetch", [_clone, _git_clone_then_pull], ids=["clone", "pull"])
def test_a_stale_lock_entry_is_skipped_and_named(product, mintd, s3_server, tmp_path, fetch) -> None:
    _shrink_fan(product)

    dest, run = fetch(mintd, product, tmp_path, json=False)

    assert run.returncode == 0, run.stderr
    warning = "skipped dvc.lock entries for 1 stage(s) dvc.yaml no longer declares, as dvc pull does: fan@q"
    assert " ".join(run.stderr.split()).count(warning) == 1, run.stderr
    fan_p = (product.producer.work / "data" / "fan_p.csv").read_bytes()
    assert _landed(dest) == {**{k: md5(b) for k, b in product.published().items()}, "data/fan_p.csv": md5(fan_p)}


RENAMED = """stages:
  make:
    cmd: echo made > data/old.csv
    outs:
      - data/old.csv
"""


def _import_and_rename(product, tmp_path: Path) -> None:
    """The producer imports `data/final` of another producer as `data/imported`,
    and renames its stage's output `data/old.csv` to `data/new.csv` in
    dvc.yaml without running it again, so `dvc.lock` keeps the old name."""
    upstream = build_local_producer(tmp_path / "upstream")
    upstream.publish({"data/final": {"u.csv": b"upstream\n"}})
    producer = product.producer
    subprocess.run([sys.executable, "-m", "dvc", "import", "-q", upstream.url, "data/final", "-o", "data/imported"],
                   cwd=producer.work, check=True)
    producer.publish_pipeline(RENAMED)
    (producer.work / "dvc.yaml").write_text(RENAMED.replace("data/old.csv", "data/new.csv"))
    _git(["add", "-A"], cwd=producer.work)
    _git(["commit", "-q", "-m", "rename the output"], cwd=producer.work)
    _git(["push", "-q"], cwd=producer.work)


@pytest.mark.parametrize("fetch", [_clone, _git_clone_then_pull], ids=["clone", "pull"])
def test_a_refused_checkout_still_lets_the_imports_land(product, mintd, s3_server, tmp_path, fetch) -> None:
    _import_and_rename(product, tmp_path)

    dest, run = fetch(mintd, product, tmp_path, json=False)

    assert run.returncode != 0, run.stderr
    assert "data/old.csv" in run.stderr
    assert (dest / "data" / "imported" / "u.csv").read_bytes() == b"upstream\n"
    assert _landed(dest) == {**{k: md5(b) for k, b in product.published().items()},
                             "data/imported/u.csv": md5(b"upstream\n")}
