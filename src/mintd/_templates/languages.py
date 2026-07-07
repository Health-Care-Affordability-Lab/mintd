"""Per-language scaffold definitions.

Ports legacy ``mintd/src/mintd/templates/languages.py`` verbatim in shape.
Each language entry returns the structure dict (dirs/files to create) and
the file list (target rel-path → template name) given a source directory.

Functions take ``source_dir`` so the caller can vary it; slice 19 hardcodes
``"code"`` to match legacy defaults.
"""

from __future__ import annotations

from typing import Any


# Each entry's ``project_files`` / ``data_files`` is a list of
# ``(target_rel_path, template_name)`` tuples that the scaffolds.py
# orchestrator joins with the per-type common file list.
LANGUAGES: dict[str, dict[str, Any]] = {
    "python": {
        "name": "python",
        "file_extension": "py",
        "project_files": lambda source_dir: [
            ("requirements.txt", "requirements_project.txt.j2"),
            (f"{source_dir}/_mintd_utils.py", "_mintd_utils.py.j2"),
            (f"{source_dir}/config.py", "config.py.j2"),
            (f"{source_dir}/02_analysis/__init__.py", "__init__.py.j2"),
            ("run_all.py", "run_all.py.j2"),
        ],
        "data_files": lambda source_dir: [
            ("requirements.txt", "requirements_data.txt.j2"),
            (f"{source_dir}/_mintd_utils.py", "_mintd_utils.py.j2"),
            (f"{source_dir}/fetch.py", "fetch.py.j2"),
            (f"{source_dir}/ingest.py", "ingest.py.j2"),
            (f"{source_dir}/validate.py", "validate.py.j2"),
        ],
    },
    "r": {
        "name": "r",
        "file_extension": "R",
        "project_files": lambda source_dir: [
            ("DESCRIPTION", "DESCRIPTION.j2"),
            ("renv.lock", "renv.lock.j2"),
            (".Rprofile", ".Rprofile.j2"),
            ("NAMESPACE", "NAMESPACE.j2"),
            (f"{source_dir}/_mintd_utils.R", "_mintd_utils.R.j2"),
            (f"{source_dir}/config.R", "config.R.j2"),
            (f"{source_dir}/02_analysis/analysis.R", "analysis.R.j2"),
            ("run_all.R", "run_all.R.j2"),
        ],
        "data_files": lambda source_dir: [
            ("DESCRIPTION", "DESCRIPTION.j2"),
            ("renv.lock", "renv.lock.j2"),
            (".Rprofile", ".Rprofile.j2"),
            ("NAMESPACE", "NAMESPACE.j2"),
            (f"{source_dir}/_mintd_utils.R", "_mintd_utils.R.j2"),
            (f"{source_dir}/fetch.R", "fetch.R.j2"),
            (f"{source_dir}/ingest.R", "ingest.R.j2"),
            (f"{source_dir}/validate.R", "validate.R.j2"),
        ],
    },
    "stata": {
        "name": "stata",
        "file_extension": "do",
        "project_files": lambda source_dir: [
            ("stata-packages.txt", "stata-packages.txt.j2"),
            (f"{source_dir}/_mintd_utils.do", "_mintd_utils.do.j2"),
            (f"{source_dir}/config.do", "config.do.j2"),
            ("run_all.do", "run_all.do.j2"),
        ],
        "data_files": lambda source_dir: [
            ("stata-packages.txt", "stata-packages.txt.j2"),
            (f"{source_dir}/_mintd_utils.do", "_mintd_utils.do.j2"),
            (f"{source_dir}/fetch.do", "fetch.do.j2"),
            (f"{source_dir}/ingest.do", "ingest.do.j2"),
            (f"{source_dir}/validate.do", "validate.do.j2"),
        ],
    },
}


def get_language_config(language: str) -> dict[str, Any]:
    """Look up a language config. Raises ``ValueError`` on unknown language."""
    cfg = LANGUAGES.get(language)
    if cfg is None:
        valid = ", ".join(sorted(LANGUAGES))
        raise ValueError(f"unknown language {language!r}; expected one of: {valid}")
    return cfg
