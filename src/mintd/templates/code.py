"""Code project template — metadata-only, no scaffold."""

from typing import Any, Dict, List, Tuple

from .base import BaseTemplate


class CodeTemplate(BaseTemplate):
    """Template for code-only repositories (libraries, packages, tools).

    Unlike data and project templates, this creates NO directory structure.
    It only drops a metadata.json for registry tracking, governance,
    and mirroring.
    """

    prefix = ""  # No prefix for code repos — they keep their own name
    template_type = "code"

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return empty structure — code repos manage their own layout."""
        return {
            "metadata.json": None,
        }

    def define_files(self) -> List[Tuple[str, str]]:
        """Return only the metadata template."""
        return [
            ("metadata.json", "metadata_code.json.j2"),
        ]
