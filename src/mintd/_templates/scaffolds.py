"""Per-project-type scaffold definitions.

Each function returns ``(dirs_to_create, files_to_render)`` where
``files_to_render`` is a list of ``(target_rel_path, template_name)``
tuples consumed by ``_render.render_scaffold``.

Ports legacy ``mintd/src/mintd/templates/{data,project,code,enclave}.py``
verbatim in shape — same dir layouts, same template names. Files marked
``gitignore.txt`` / ``dvcignore.txt`` are static (not Jinja-rendered);
the renderer copies them as-is.

For data + project types the language-specific files (source code,
requirements / DESCRIPTION / stata-packages, etc.) come from
``languages.get_language_config(language)``. Code + enclave do not
branch on language in legacy and don't here either.
"""

from __future__ import annotations

from .languages import get_language_config


def _common_data_dirs(source_dir: str) -> list[str]:
    return [
        "data/raw",
        "data/intermediate",
        "data/final",
        "schemas/v1",
        "scripts",
        source_dir,
    ]


def _common_data_files() -> list[tuple[str, str]]:
    return [
        ("README.md", "README_data.md.j2"),
        ("metadata.json", "metadata.json.j2"),
        (".gitignore", "gitignore.txt"),
        (".dvcignore", "dvcignore.txt"),
        (".pre-commit-config.yaml", "pre-commit-config.yaml.j2"),
        ("scripts/check-dvc-sync.sh", "check-dvc-sync.sh.j2"),
        ("scripts/check-env-lockfiles.sh", "check-env-lockfiles.sh.j2"),
        ("dvc_vars.yaml", "dvc_vars.yaml.j2"),
        ("dvc.yaml", "dvc_data.yaml.j2"),
    ]


def scaffold_data(language: str, name: str, full_name: str, source_dir: str = "code") -> tuple[list[str], list[tuple[str, str]]]:
    del name, full_name  # accepted for symmetry; templates pull these from context
    lang_cfg = get_language_config(language)
    files = _common_data_files() + lang_cfg["data_files"](source_dir)
    return _common_data_dirs(source_dir), files


def _common_project_dirs(source_dir: str) -> list[str]:
    return [
        "data/raw",
        "data/analysis",
        "data/enclave-out",
        f"{source_dir}/01_data_prep",
        f"{source_dir}/02_analysis",
        f"{source_dir}/03_tables",
        f"{source_dir}/04_figures",
        "notebooks",
        "results/figures",
        "results/tables",
        "results/estimates",
        "results/presentations",
        "docs",
        "references",
        "tests",
        "scripts",
    ]


def _common_project_files() -> list[tuple[str, str]]:
    return [
        ("README.md", "README_project.md.j2"),
        ("metadata.json", "metadata.json.j2"),
        ("citations.md", "citations.md.j2"),
        (".gitignore", "gitignore.txt"),
        (".dvcignore", "dvcignore.txt"),
        (".pre-commit-config.yaml", "pre-commit-config.yaml.j2"),
        ("scripts/check-dvc-sync.sh", "check-dvc-sync.sh.j2"),
        ("scripts/check-env-lockfiles.sh", "check-env-lockfiles.sh.j2"),
    ]


def scaffold_project(language: str, name: str, full_name: str, source_dir: str = "code") -> tuple[list[str], list[tuple[str, str]]]:
    del name, full_name
    lang_cfg = get_language_config(language)
    files = _common_project_files() + lang_cfg["project_files"](source_dir)
    return _common_project_dirs(source_dir), files


def scaffold_code(language: str, name: str, full_name: str, source_dir: str = "code") -> tuple[list[str], list[tuple[str, str]]]:
    """Code repos are metadata-only — no language-specific scaffold.

    Slice 19 emits ``metadata.json`` only (no .dvcignore — the legacy
    `--with-data` opt-in is a separate slice). The `language` arg is
    accepted for signature symmetry but unused.
    """
    del language, name, full_name, source_dir
    return [], [
        ("metadata.json", "metadata_code.json.j2"),
    ]


def scaffold_enclave(language: str, name: str, full_name: str, source_dir: str = "code") -> tuple[list[str], list[tuple[str, str]]]:
    """Enclave scaffold — language arg ignored (always Python toolchain inside)."""
    del language, name, full_name, source_dir
    dirs = ["data", "src", "scripts", "transfers"]
    files = [
        ("README.md", "README_enclave.md.j2"),
        ("metadata.json", "metadata.json.j2"),
        ("enclave_manifest.yaml", "enclave_manifest.yaml.j2"),
        ("requirements.txt", "requirements_enclave.txt.j2"),
        ("enclave_cli.py", "enclave_cli.py.j2"),
        (".gitignore", "gitignore.txt"),
        (".dvcignore", "dvcignore.txt"),
        ("src/__init__.py", "__init__.py.j2"),
        ("src/registry.py", "registry.py.j2"),
        ("src/download.py", "download.py.j2"),
        ("src/transfer.py", "transfer.py.j2"),
        ("scripts/pull_data.sh", "pull_data.sh.j2"),
        ("scripts/package_transfer.sh", "package_transfer.sh.j2"),
        ("scripts/unpack_transfer.sh", "unpack_transfer.sh.j2"),
        ("scripts/verify_transfer.sh", "verify_transfer.sh.j2"),
    ]
    return dirs, files


_DISPATCH = {
    "data": scaffold_data,
    "project": scaffold_project,
    "code": scaffold_code,
    "enclave": scaffold_enclave,
}


def dispatch(project_type: str):
    """Return the scaffold function for ``project_type``."""
    try:
        return _DISPATCH[project_type]
    except KeyError:
        valid = ", ".join(sorted(_DISPATCH))
        raise ValueError(f"unknown project_type {project_type!r}; expected one of: {valid}")
