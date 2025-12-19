#!/usr/bin/env python3
"""Setup script for mint."""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="mint",
    version="0.1.0",
    author="mint development team",
    author_email="mint@example.com",
    description="Lab project scaffolding tool",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Cooper-lab/mint",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    license="MIT",
    keywords="research data version-control git dvc stata python cli",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Archiving",
    ],
    include_package_data=True,
    install_requires=[
        "click>=8.0",
        "gitpython>=3.1",
        "jinja2>=3.0",
        "rich>=13.0",
        "keyring>=24.0",
        "boto3>=1.28",
        "pyyaml>=6.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "ruff>=0.1",
            "mypy>=1.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "mint=mint.cli:main",
        ],
    },
)