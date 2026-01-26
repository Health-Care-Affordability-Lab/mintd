"""Utilities for loading custom templates."""

import os
import sys
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, Type

from ..templates.base import BaseTemplate


def get_custom_template_dir() -> Path:
    """Get the directory for custom templates.
    
    Priority:
    1. MINT_TEMPLATES_DIR environment variable
    2. ~/.mint/templates
    """
    env_dir = os.environ.get("MINT_TEMPLATES_DIR")
    if env_dir:
        return Path(env_dir)
    
    return Path.home() / ".mint" / "templates"


def load_custom_templates() -> Dict[str, Type[BaseTemplate]]:
    """Load custom templates from the custom template directory.
    
    Returns:
        Dict mapping template prefix to template class.
    """
    templates: Dict[str, Type[BaseTemplate]] = {}
    template_dir = get_custom_template_dir()
    
    if not template_dir.exists():
        return templates

    # Add template directory into python path to allow relative imports within custom templates if needed
    sys.path.append(str(template_dir))

    try:
        # Find all .py files
        for file_path in template_dir.glob("*.py"):
            if file_path.name.startswith("_"):
                continue

            try:
                # Load module
                module_name = f"mint_custom_{file_path.stem}"
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Scan for BaseTemplate subclasses
                    for name, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and 
                            issubclass(obj, BaseTemplate) and 
                            obj is not BaseTemplate and
                            hasattr(obj, 'prefix')):
                            
                            templates[obj.prefix] = obj
            except Exception as e:
                # Log warning but continue?
                print(f"Warning: Failed to load custom template from {file_path}: {e}")
                
    finally:
        # Remove from sys.path
        if str(template_dir) in sys.path:
            sys.path.remove(str(template_dir))

    return templates
