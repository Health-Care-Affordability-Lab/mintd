"""Tests for the template loader utilities."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from mintd.utils.loader import get_custom_template_dir, load_custom_templates
from mintd.templates.base import BaseTemplate


class TestGetCustomTemplateDir:
    """Tests for get_custom_template_dir function."""

    @patch.dict(os.environ, {"MINT_TEMPLATES_DIR": "/custom/templates"})
    def test_env_variable_takes_priority(self):
        """Test that environment variable takes priority."""
        result = get_custom_template_dir()
        assert result == Path("/custom/templates")

    @patch.dict(os.environ, {}, clear=True)
    def test_default_directory(self):
        """Test default directory when no env var set."""
        os.environ.pop("MINT_TEMPLATES_DIR", None)
        result = get_custom_template_dir()
        assert result == Path.home() / ".mint" / "templates"


class TestLoadCustomTemplates:
    """Tests for load_custom_templates function."""

    @patch("mintd.utils.loader.get_custom_template_dir")
    def test_empty_when_dir_not_exists(self, mock_get_dir):
        """Test returns empty dict when template dir doesn't exist."""
        mock_get_dir.return_value = Path("/nonexistent/templates")

        result = load_custom_templates()

        assert result == {}

    def test_load_valid_template(self):
        """Test loading a valid custom template."""
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)

            # Create a valid template file
            template_code = '''
from mintd.templates.base import BaseTemplate
from pathlib import Path
from typing import Dict, List, Tuple, Any

class CustomTemplate(BaseTemplate):
    prefix = "custom_"

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        return {"README.md": None}

    def define_files(self) -> List[Tuple[str, str]]:
        return [("README.md", "README.md.j2")]
'''
            (template_dir / "custom.py").write_text(template_code)

            with patch("mintd.utils.loader.get_custom_template_dir") as mock_get_dir:
                mock_get_dir.return_value = template_dir

                result = load_custom_templates()

                assert "custom_" in result
                assert issubclass(result["custom_"], BaseTemplate)

    def test_skip_underscore_files(self):
        """Test that files starting with underscore are skipped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)

            # Create file starting with underscore
            (template_dir / "_private.py").write_text("# private file")

            with patch("mintd.utils.loader.get_custom_template_dir") as mock_get_dir:
                mock_get_dir.return_value = template_dir

                result = load_custom_templates()

                assert result == {}

    def test_handle_invalid_template(self):
        """Test handling of invalid template files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)

            # Create an invalid Python file
            (template_dir / "invalid.py").write_text("this is not valid python {{{")

            with patch("mintd.utils.loader.get_custom_template_dir") as mock_get_dir:
                mock_get_dir.return_value = template_dir

                # Should not raise, just return empty
                result = load_custom_templates()

                assert result == {}

    def test_cleanup_sys_path(self):
        """Test that sys.path is cleaned up after loading."""
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)

            with patch("mintd.utils.loader.get_custom_template_dir") as mock_get_dir:
                mock_get_dir.return_value = template_dir

                initial_path_len = len(sys.path)
                load_custom_templates()

                # sys.path should be back to original length
                assert len(sys.path) == initial_path_len

    def test_template_without_prefix_skipped(self):
        """Test that templates without prefix attribute are skipped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = Path(temp_dir)

            # Create a template without prefix
            template_code = '''
from mintd.templates.base import BaseTemplate
from pathlib import Path
from typing import Dict, List, Tuple, Any

class NoPrefix(BaseTemplate):
    # No prefix attribute

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        return {}

    def define_files(self) -> List[Tuple[str, str]]:
        return []
'''
            (template_dir / "noprefix.py").write_text(template_code)

            with patch("mintd.utils.loader.get_custom_template_dir") as mock_get_dir:
                mock_get_dir.return_value = template_dir

                result = load_custom_templates()

                # Template without prefix should be skipped
                assert len(result) == 0
