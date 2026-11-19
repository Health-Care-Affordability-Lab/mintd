"""What the stories share: the installed mintd, a published product and a
fresh project to bring it into.

Stories run apart from the unit suite (`addopts` ignores this folder):
`uv run pytest tests/stories`, which builds and installs the wheel once per
session unless `MINTD_BIN` names a binary.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from tests._harness.consumer import build_consumer_project
from tests._harness.git import _git
from tests._harness.mintd import Mintd, mintd, mintd_bin  # noqa: F401
from tests._harness.producer import LocalProducer, build_local_producer
from tests._harness.s3 import BUCKET, S3Server


@dataclasses.dataclass
class Product:
    """A made-up product: `data/final/` is its primary, `data/extra/` another
    output, and `data/raw/` a folder it tracks that no output declares."""

    producer: LocalProducer
    name = "harness-demo"
    prefix = "lab/harness-demo"  # where its objects sit in the bucket
    outs = {
        "data/final": {"a.csv": b"alpha\n", "b.csv": b"beta\n"},
        "data/extra": {"c.csv": b"gamma\n"},
        "data/raw": {"r.csv": b"raw\n"},
    }

    def published(self, *outs: str) -> dict[str, bytes]:
        """`{S3 key under prefix: bytes}` for each file of `outs`, every out when none are named."""
        return {f"{out}/{name}": body for out in outs or self.outs for name, body in self.outs[out].items()}


def register(registry: Path, meta: dict, work: Path) -> None:
    """Merge `meta` into the scratch registry as its catalog entry, as an
    approved registration PR would."""
    _git(["clone", str(registry), str(work)])
    entry = work / "catalog" / meta["project"]["type"] / f"{meta['project']['name']}.yaml"
    entry.parent.mkdir(parents=True, exist_ok=True)  # git keeps no empty folder
    entry.write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-m", f"register {meta['project']['name']}"], cwd=work)
    _git(["push", "origin", "main"], cwd=work)


@pytest.fixture
def product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, s3_server: S3Server,
    remote_registry_empty: Path, mintd: Mintd,  # noqa: F811
) -> Product:
    """A `Product`, published: its outputs on S3, pinned by version ID, and its
    entry in the registry `mintd`'s config names. The S3 server's records start
    empty, so they hold only what the story's commands did."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")  # for the in-process dvc push
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    product = Product(build_local_producer(tmp_path / "prod", remote={
        "url": f"s3://{BUCKET}/{Product.prefix}", "endpointurl": s3_server.endpoint, "version_aware": "true",
    }))
    producer = product.producer
    meta = producer.metadata()
    meta["project"].update(name=product.name, full_name=f"data_{product.name}")
    meta["repository"]["github_url"] = producer.url
    producer._write_metadata(meta)
    producer.add_output("data/extra/")
    producer.publish(product.outs)
    register(remote_registry_empty, producer.metadata(), tmp_path / "registry-work")
    mintd.config.write_text(
        f"registry_url: {remote_registry_empty}\ncache_dir: {tmp_path / 'mintd-cache'}\n", encoding="utf-8"
    )
    s3_server.puts.clear()
    s3_server.gets.clear()
    return product


@pytest.fixture
def fresh_project(tmp_path: Path):
    """A project as a researcher has it before any import: metadata.json, git
    and dvc initialized, all committed. One folder per call.

    `mintd init` would make it, but init refuses to run without a terminal."""
    from dvc.repo import Repo

    def build(name: str = "project") -> Path:
        root = build_consumer_project(tmp_path / name)
        meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        meta["project"].update(type="project", name="analysis", full_name="prj_analysis")
        meta["data_products"] = {"primary": None, "outputs": []}  # as `mintd init` writes it
        (root / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        _git(["init", "-b", "main", str(root)])
        Repo.init(str(root)).close()
        _git(["add", "-A"], cwd=root)
        _git(["commit", "-m", "fresh project"], cwd=root)
        return root

    return build
