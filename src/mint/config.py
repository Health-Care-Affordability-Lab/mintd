"""Configuration management for mint."""

import os
import json
from pathlib import Path
from typing import Optional, Tuple

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False


# Configuration file location
CONFIG_DIR = Path.home() / ".mint"
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def get_storage_credentials() -> Tuple[str, str]:
    """Retrieve S3 credentials from keychain or environment variables.

    Returns:
        Tuple of (access_key, secret_key)

    Raises:
        ValueError: If credentials are not found
    """
    # Try keychain first (if available)
    if KEYRING_AVAILABLE:
        access_key = keyring.get_password("mint", "aws_access_key_id")
        secret_key = keyring.get_password("mint", "aws_secret_access_key")

        if access_key and secret_key:
            return access_key, secret_key

    # Fall back to environment variables
    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("MINT_AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("MINT_AWS_SECRET_ACCESS_KEY")

    if access_key and secret_key:
        return access_key, secret_key

    raise ValueError(
        "Storage credentials not found. Please run 'mint config' to set them up, "
        "or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables."
    )


def get_registry_url() -> str:
    """Get registry URL from environment variable or config file.

    Returns:
        Registry repository URL

    Raises:
        ValueError: If registry URL is not configured
    """
    # Check environment variable first (highest priority)
    url = os.getenv("MINT_REGISTRY_URL")
    if url:
        return url.strip()

    # Fall back to config file
    config = get_config()
    registry_config = config.get("registry", {})
    url = registry_config.get("url")

    if url:
        return url.strip()

    raise ValueError(
        "Registry URL not configured. Please set MINT_REGISTRY_URL environment variable "
        "or configure registry.url in ~/.mint/config.yaml"
    )


def set_storage_credentials(access_key: str, secret_key: str) -> None:
    """Store S3 credentials securely in keychain.

    Args:
        access_key: AWS access key ID
        secret_key: AWS secret access key
    """
    if not KEYRING_AVAILABLE:
        raise RuntimeError(
            "keyring package is required for secure credential storage. "
            "Install with: pip install keyring"
        )

    keyring.set_password("mint", "aws_access_key_id", access_key)
    keyring.set_password("mint", "aws_secret_access_key", secret_key)


def get_config() -> dict:
    """Load configuration from ~/.mint/config.yaml.

    Returns:
        Configuration dictionary
    """
    if not CONFIG_FILE.exists():
        return _get_default_config()

    try:
        import yaml
        with open(CONFIG_FILE, "r") as f:
            return yaml.safe_load(f) or _get_default_config()
    except ImportError:
        # Fallback to JSON if yaml not available
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return _get_default_config()


def save_config(config: dict) -> None:
    """Save configuration to ~/.mint/config.yaml.

    Args:
        config: Configuration dictionary to save
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        import yaml
        with open(CONFIG_FILE, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)
    except ImportError:
        # Fallback to JSON if yaml not available
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)


def _get_default_config() -> dict:
    """Get default configuration values."""
    return {
        "storage": {
            "provider": "s3",
            "endpoint": "",
            "region": "",
            "bucket_prefix": "",
            "versioning": True,
        },
        "registry": {
            "url": "https://github.com/cooper-lab/data-commons-registry",
            "org": "cooper-lab",
            "default_branch": "main",
        },
        "defaults": {
            "author": "",
            "organization": "",
        }
    }


def init_config() -> None:
    """Interactive first-time setup - prompts for all required values."""
    from rich.console import Console
    from rich.prompt import Prompt, Confirm

    console = Console()

    console.print("\n[bold blue]mint Configuration Setup[/bold blue]")
    console.print("Let's set up your storage configuration.\n")

    config = _get_default_config()

    # Storage provider (default to S3)
    provider = Prompt.ask(
        "Storage provider",
        default="s3",
        choices=["s3"]
    )
    config["storage"]["provider"] = provider

    # S3 endpoint
    endpoint = Prompt.ask(
        "Storage endpoint (leave blank for AWS S3)",
        default=""
    )
    config["storage"]["endpoint"] = endpoint

    # Region
    region = Prompt.ask(
        "Region (leave blank for default)",
        default=""
    )
    config["storage"]["region"] = region

    # Bucket prefix
    bucket_prefix = Prompt.ask(
        "Bucket prefix (e.g., your lab name)",
        default=""
    )
    config["storage"]["bucket_prefix"] = bucket_prefix

    # Author
    author = Prompt.ask(
        "Your name",
        default=""
    )
    config["defaults"]["author"] = author

    # Organization
    organization = Prompt.ask(
        "Organization/lab name",
        default=""
    )
    config["defaults"]["organization"] = organization

    # Registry settings
    console.print("\n[bold blue]Registry Configuration[/bold blue]")
    console.print("For project registration in the Data Commons Registry:")

    registry_url = Prompt.ask(
        "Registry repository URL",
        default="https://github.com/cooper-lab/data-commons-registry"
    )
    config["registry"]["url"] = registry_url

    registry_org = Prompt.ask(
        "GitHub organization",
        default="cooper-lab"
    )
    config["registry"]["org"] = registry_org

    # Save configuration
    save_config(config)
    console.print(f"\n✅ Configuration saved to {CONFIG_FILE}")

    # Set up credentials
    console.print("\n[bold blue]Storage Credentials[/bold blue]")
    setup_creds = Confirm.ask("Set up storage credentials now?", default=True)

    if setup_creds:
        access_key = Prompt.ask("AWS Access Key ID")
        secret_key = Prompt.ask("AWS Secret Access Key", password=True)

        try:
            set_storage_credentials(access_key, secret_key)
            console.print("✅ Credentials stored securely")
        except RuntimeError as e:
            console.print(f"⚠️  Could not store credentials securely: {e}")
            console.print("You can set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables instead.")

    # Registry configuration info
    console.print("\n[bold blue]Registry Configuration[/bold blue]")
    console.print(f"Registry URL: {registry_url}")
    console.print("Registry access uses SSH keys and GitHub CLI (gh). Make sure you have:")
    console.print("  - SSH key configured for GitHub")
    console.print("  - GitHub CLI (gh) installed and authenticated")
    console.print("  - Push access to the registry repository")

    console.print("\n🎉 Setup complete! You can now use mint to create projects.")


def validate_config() -> bool:
    """Validate that the current configuration is complete.

    Returns:
        True if configuration is valid and complete
    """
    config = get_config()

    # Check required fields
    storage = config.get("storage", {})
    if not storage.get("bucket_prefix"):
        return False

    try:
        get_storage_credentials()
    except ValueError:
        return False

    # Check registry URL is configured
    try:
        get_registry_url()
    except ValueError:
        return False

    return True