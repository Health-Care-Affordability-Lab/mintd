"""The docs name only fields the model has.

A README that told producers to edit a `contact` field the schema never had,
and listed `aws_profile_name` as a config key `config setup` rejects, cost
each new producer rounds of check errors. These tests read the tracked docs
and resolve what they name against `Metadata` and `Config`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

import mintd
from mintd._config import Config
from mintd.model import DataProductOutput, DataProducts, Metadata, _unwrap_container, field_metadata

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "vocab.md", Path(mintd.__file__).parent / "files" / "README_data.md.j2"]

#: Dotted paths the docs name on purpose though the model lacks them.
NOT_FIELDS = {
    "ownership.slack",  # vocab.md: an example of a key a user adds, which publish must keep
}
#: The JSON name of a field whose Python name differs.
ALIASES = {f.alias: name for name, f in Metadata.model_fields.items() if f.alias}
FILE_SUFFIX = re.compile(r"\.(json|ya?ml|md|lock|dvc|txt|py|csv)$")
PATH_SHAPE = re.compile(r"^[A-Za-z_]\w*(\[\])?(\.([A-Za-z_]\w*(\[\])?|\*))+$")
SPAN = re.compile(r"`([^`\n]+)`")


def _field_paths(doc: Path) -> list[tuple[int, str]]:
    """``(line, span)`` for each backticked span in ``doc`` shaped like a
    metadata field path: its first segment a ``Metadata`` field (or alias),
    or ``outputs[]``. Fenced code blocks count: the produce example's comments
    name fields too."""
    tops = set(Metadata.model_fields) | set(ALIASES)
    found = []
    for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        for span in SPAN.findall(line):
            path = span.rstrip(":")
            if not PATH_SHAPE.match(path) or FILE_SUFFIX.search(path) or path in NOT_FIELDS:
                continue
            if path.split(".")[0].removesuffix("[]") in tops | {"outputs"}:
                found.append((n, path))
    return found


def _resolve(path: str) -> None:
    """Raise KeyError unless ``path`` names a ``Metadata`` field: ``[]``
    steps into a list, ``outputs`` is short for ``data_products.outputs``,
    and ``x.*`` needs ``x`` to be a model."""
    parts = path.replace("[]", "").split(".")
    if parts[0] == "outputs":
        parts.insert(0, "data_products")
    parts[0] = ALIASES.get(parts[0], parts[0])
    if parts[-1] != "*":
        field_metadata(Metadata, ".".join(parts))
        return
    model: object = Metadata
    for part in parts[:-1]:
        if not (isinstance(model, type) and issubclass(model, BaseModel)) or part not in model.model_fields:
            raise KeyError(f"Field '{part}' not found")
        model = _unwrap_container(model.model_fields[part].annotation)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise KeyError(f"{path}: not a model")


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_every_field_path_the_docs_name_exists(doc: Path) -> None:
    """Mutation: rename `metadata.description` on README's produce line, or
    write `metadata.contact` there -> red."""
    paths = _field_paths(doc)
    bad = []
    for n, path in paths:
        try:
            _resolve(path)
        except KeyError as e:
            bad.append(f"{doc.name}:{n} `{path}`: {e}")
    assert bad == []
    if doc.name == "README.md":
        assert (any(p == "metadata.description" for _, p in paths)
                and any(p == "data_products.outputs[]" for _, p in paths))


@pytest.mark.parametrize("path, ok", [
    ("metadata.description", True), ("data_products.outputs[].last_published", True),
    ("outputs[].path", True), ("storage.*", True), ("schema.location", True), ("schema_.location", True),
    ("metadata.contact", False), ("outputs[].contact", False), ("ownership.team.*", False), ("nope.*", False),
])
def test_the_resolver_tells_fields_from_names_the_model_lacks(path: str, ok: bool) -> None:
    if ok:
        _resolve(path)
    else:
        with pytest.raises(KeyError):
            _resolve(path)


def test_the_readme_example_entry_is_one_the_model_accepts() -> None:
    """Every key a model field (the model ignores unknown keys, so validation
    alone would pass a stray one), and the entry valid without
    `last_published`.

    Mutations: drop `description` from the example, or add `"contact"` -> red."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### What metadata.json needs", 1)[1]
    example = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    dp = example["data_products"]
    assert set(dp) <= set(DataProducts.model_fields)
    for entry in dp["outputs"]:
        assert set(entry) <= set(DataProductOutput.model_fields)
        DataProductOutput.model_validate(entry)
    assert DataProducts.model_validate(dp).primary in [o["path"] for o in dp["outputs"]]


def test_the_config_knobs_the_readme_names_are_config_keys() -> None:
    """`config setup --set` accepts only `Config` fields.

    Mutations: put `aws_profile_name` back -> red; drop the paragraph -> red."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    paragraphs = [p for p in readme.split("\n\n") if "`~/.config/mintd/config.yaml`" in p and "Knobs" in p]
    assert len(paragraphs) == 1, "the README's config-knobs paragraph is gone"
    knobs = [s.rstrip(":") for s in SPAN.findall(paragraphs[0]) if "/" not in s]
    assert knobs
    assert [k for k in knobs if k not in Config.model_fields] == []
