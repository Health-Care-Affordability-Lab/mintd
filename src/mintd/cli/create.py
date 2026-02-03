"""Create command group for project scaffolding."""

import re

import click

from .main import main
from .utils import console


@main.group()
def create():
    """Create a new project."""
    pass


@create.command(name="data")
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--bucket", help="Override bucket name for DVC remote")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root")
@click.option("--admin-team", help="Override default admin team")
@click.option("--researcher-team", help="Override default researcher team")
@click.option("--public", is_flag=True, help="Mark as public data")
@click.option("--contract", help="Mark as contract data (provide contract slug)")
@click.option("--private", is_flag=True, help="Mark as private/lab data (default)")
@click.option("--contract-info", help="Description or link to contract")
@click.option("--team", help="Owning team slug")
def create_data(name, path, lang, no_git, no_dvc, bucket, register, use_current_repo, admin_team, researcher_team, public, contract, private, contract_info, team):
    """Create a data product repository (data_{name})."""
    from ..api import create_project

    classification = "private"
    contract_slug = None

    if public:
        classification = "public"
    elif contract:
        classification = "contract"
        contract_slug = contract
    elif private:
        classification = "private"
    else:
        console.print()
        console.print("[bold]Governance Configuration[/bold]")
        classification = click.prompt(
            "Data Classification",
            type=click.Choice(["public", "private", "contract"]),
            default="private"
        )
        if classification == "contract":
            contract_slug = click.prompt("Contract Slug (short-name for URL)")

    if classification == "contract" and not contract_info:
        contract_info = click.prompt("Contract Info (URL or description)", default="")

    with console.status("Scaffolding project..."):
        try:
            result = create_project(
                project_type="data", name=name, path=path, language=lang,
                init_git=not no_git, init_dvc=not no_dvc, bucket_name=bucket,
                register_project=register, use_current_repo=use_current_repo,
                admin_team=admin_team, researcher_team=researcher_team,
                classification=classification, team=team,
                contract_slug=contract_slug, contract_info=contract_info,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")
            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command()
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--bucket", help="Override bucket name for DVC remote")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root")
@click.option("--admin-team", help="Override default admin team")
@click.option("--researcher-team", help="Override default researcher team")
@click.option("--public", is_flag=True, help="Mark as public data")
@click.option("--contract", help="Mark as contract data (provide contract slug)")
@click.option("--private", is_flag=True, help="Mark as private/lab data (default)")
@click.option("--contract-info", help="Description or link to contract")
@click.option("--team", help="Owning team slug")
def project(name, path, lang, no_git, no_dvc, bucket, register, use_current_repo, admin_team, researcher_team, public, contract, private, contract_info, team):
    """Create a project repository (prj__{name})."""
    from ..api import create_project

    classification = "private"
    contract_slug = None

    if public:
        classification = "public"
    elif contract:
        classification = "contract"
        contract_slug = contract
    elif private:
        classification = "private"
    else:
        console.print()
        console.print("[bold]Governance Configuration[/bold]")
        classification = click.prompt(
            "Data Classification",
            type=click.Choice(["public", "private", "contract"]),
            default="private"
        )
        if classification == "contract":
            contract_slug = click.prompt("Contract Slug (short-name for URL)")

    if classification == "contract" and not contract_info:
        contract_info = click.prompt("Contract Info (URL or description)", default="")

    with console.status("Scaffolding project..."):
        try:
            result = create_project(
                project_type="project", name=name, path=path, language=lang,
                init_git=not no_git, init_dvc=not no_dvc, bucket_name=bucket,
                register_project=register, use_current_repo=use_current_repo,
                admin_team=admin_team, researcher_team=researcher_team,
                classification=classification, team=team,
                contract_slug=contract_slug, contract_info=contract_info,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")
            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command()
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--bucket", help="Override bucket name for DVC remote")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root")
@click.option("--admin-team", help="Override default admin team")
@click.option("--researcher-team", help="Override default researcher team")
@click.option("--public", is_flag=True, help="Mark as public data")
@click.option("--contract", help="Mark as contract data (provide contract slug)")
@click.option("--private", is_flag=True, help="Mark as private/lab data (default)")
@click.option("--contract-info", help="Description or link to contract")
@click.option("--team", help="Owning team slug")
def infra(name, path, lang, no_git, no_dvc, bucket, register, use_current_repo, admin_team, researcher_team, public, contract, private, contract_info, team):
    """Create an infrastructure repository (infra_{name})."""
    from ..api import create_project

    classification = "private"
    contract_slug = None

    if public:
        classification = "public"
    elif contract:
        classification = "contract"
        contract_slug = contract
    elif private:
        classification = "private"
    else:
        console.print()
        console.print("[bold]Governance Configuration[/bold]")
        classification = click.prompt(
            "Data Classification",
            type=click.Choice(["public", "private", "contract"]),
            default="private"
        )
        if classification == "contract":
            contract_slug = click.prompt("Contract Slug (short-name for URL)")

    if classification == "contract" and not contract_info:
        contract_info = click.prompt("Contract Info (URL or description)", default="")

    with console.status("Scaffolding project..."):
        try:
            result = create_project(
                project_type="infra", name=name, path=path, language=lang,
                init_git=not no_git, init_dvc=not no_dvc, bucket_name=bucket,
                register_project=register, use_current_repo=use_current_repo,
                admin_team=admin_team, researcher_team=researcher_team,
                classification=classification, team=team,
                contract_slug=contract_slug, contract_info=contract_info,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")
            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command(name="enclave")
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--registry-url", required=False, help="Data Commons Registry GitHub URL")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
def create_enclave(name, path, registry_url, no_git):
    """Create a secure data enclave workspace (enclave_{name})."""
    from ..config import CONFIG_FILE, get_config, save_config

    if not registry_url:
        config = get_config()
        config_registry_url = config.get("registry", {}).get("url", "")
        config_exists = CONFIG_FILE.exists()

        if config_exists and config_registry_url:
            registry_url = config_registry_url
        else:
            console.print()
            console.print("[bold yellow]Registry URL not configured.[/bold yellow]")
            console.print("The enclave needs a Data Commons Registry URL to pull data from.")
            console.print()
            registry_url = click.prompt("Enter the registry URL (e.g., https://github.com/org/data-registry)")
            if click.confirm("Save this registry URL to your mintd config for future use?", default=True):
                config["registry"]["url"] = registry_url
                save_config(config)
                console.print(f"✅ Registry URL saved to {CONFIG_FILE}", style="green")

    if not re.match(r'^https://github\.com/[^/]+/[^/]+/?$', registry_url):
        console.print("❌ Invalid registry URL format. Expected: https://github.com/org/repo", style="red")
        raise click.Abort()

    from ..api import create_project

    with console.status("Scaffolding enclave..."):
        try:
            result = create_project(
                project_type="enclave", name=name, path=path, language="python",
                init_git=not no_git, init_dvc=False, bucket_name=None,
                register_project=False, use_current_repo=False, registry_url=registry_url,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")
            console.print(f"   Registry: {registry_url}", style="dim")
            console.print()
            console.print("Next steps:")
            console.print("  1. cd " + str(result.path))
            console.print("  2. Run 'mintd enclave add <repo-name>' to add approved data products")
            console.print("  3. Run 'python enclave_cli.py pull --all' to download data")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command()
@click.argument("template_name")
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", default="python", help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root")
def custom(template_name, name, path, lang, no_git, no_dvc, register, use_current_repo):
    """Create a project from a custom template."""
    from ..api import create_project

    with console.status(f"Scaffolding {template_name} project..."):
        try:
            result = create_project(
                project_type=template_name, name=name, path=path, language=lang,
                init_git=not no_git, init_dvc=not no_dvc,
                register_project=register, use_current_repo=use_current_repo,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")
            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


def register_custom_commands():
    """Register CLI commands for custom templates."""
    try:
        from ..utils.loader import load_custom_templates
        custom_templates = load_custom_templates()

        for prefix, _ in custom_templates.items():
            cmd_name = prefix.rstrip("_")
            if cmd_name in create.commands:
                continue

            def create_command_func(cmd_name_val):
                @create.command(name=cmd_name_val, help=f"Create a {cmd_name_val} project ({prefix}*).")
                @click.option("--name", "-n", required=True, help="Project name")
                @click.option("--path", "-p", default=".", help="Output directory")
                @click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True)
                @click.option("--no-git", is_flag=True)
                @click.option("--no-dvc", is_flag=True)
                @click.option("--bucket", help="Override bucket name")
                @click.option("--register", is_flag=True)
                @click.option("--use-current-repo", is_flag=True)
                @click.option("--admin-team", help="Override admin team")
                @click.option("--researcher-team", help="Override researcher team")
                def custom_cmd(name, path, lang, no_git, no_dvc, bucket, register, use_current_repo, admin_team, researcher_team):
                    from ..api import create_project
                    with console.status("Scaffolding project..."):
                        try:
                            result = create_project(
                                project_type=cmd_name_val, name=name, path=path, language=lang,
                                init_git=not no_git, init_dvc=not no_dvc, bucket_name=bucket,
                                register_project=register, use_current_repo=use_current_repo,
                                admin_team=admin_team, researcher_team=researcher_team,
                            )
                            console.print(f"✅ Created: {result.full_name}", style="green")
                            console.print(f"   Location: {result.path}", style="dim")
                        except Exception as e:
                            console.print(f"❌ Error: {e}", style="red")
                            raise click.Abort()
                return custom_cmd
            create_command_func(cmd_name)
    except Exception:
        pass
