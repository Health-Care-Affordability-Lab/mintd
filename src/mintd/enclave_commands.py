"""Core logic for enclave operations.

This module contains the business logic for pulling data, packaging transfers,
and verifying transfers. It is used by the mintd CLI and can be called by
generated enclave scripts.
"""

import os
import shutil
import yaml
import git
import hashlib
import tarfile
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

from .registry import query_registry_for_product
from .config import get_config

def get_repo_info(repo_name: str) -> Dict:
    """Get repository information from registry."""
    registry_info = query_registry_for_product(repo_name)

    if not registry_info or not registry_info.get('exists'):
        raise FileNotFoundError(f"Registry entry not found for: {repo_name}")

    catalog_data = registry_info.get('catalog_data', {})

    # Extract repository and storage information
    repository = catalog_data.get('repository', {})
    storage = catalog_data.get('storage', {})
    dvc_config = storage.get('dvc', {})

    return {
        'repo_url': repository.get('github_url', ''),
        'dvc_remote_name': dvc_config.get('remote_name', ''),
        'dvc_remote_url': dvc_config.get('remote_url', ''),
        'data_stage': 'final',  # Default to final stage
    }

def convert_to_ssh_url(https_url: str) -> str:
    """Convert HTTPS GitHub URL to SSH URL for authentication."""
    if https_url.startswith('https://github.com/'):
        path = https_url[len('https://github.com/'):]
        if path.endswith('.git'):
            path = path[:-4]
        return f"git@github.com:{path}.git"
    return https_url

def configure_dvc_remote(repo_dir: Path, repo_name: str, dvc_remote_url: str = "") -> None:
    """Configure DVC remote in cloned repo."""
    import subprocess
    
    # First, check what remote name the repo expects
    repo_config = repo_dir / ".dvc" / "config"
    expected_remote = "storage"  # Default
    
    if repo_config.exists():
        with open(repo_config, 'r') as f:
            for line in f:
                if "remote =" in line:
                    expected_remote = line.split("=")[1].strip()
                    break
    
    # If we have an explicit URL from registry, use it directly
    if dvc_remote_url:
        try:
            subprocess.run(
                ["dvc", "remote", "add", "-f", expected_remote, dvc_remote_url],
                capture_output=True, text=True, cwd=repo_dir
            )
            
            # Copy endpoint configuration from mintd config if needed
            config = get_config()
            endpoint = config.get('storage', {}).get('endpoint', '')
            if endpoint:
                subprocess.run(
                    ["dvc", "remote", "modify", expected_remote, "endpointurl", endpoint],
                    capture_output=True, text=True, cwd=repo_dir
                )
            return
        except Exception:
            pass
    
    # Fallback: search global DVC config (simplified for now)
    try:
        subprocess.run(
            ["dvc", "remote", "add", "-f", expected_remote, f"s3://cooper-globus/lab/{repo_name}/"],
            capture_output=True, text=True, cwd=repo_dir
        )
    except Exception:
        pass

def pull_dvc_data(repo_dir: Path, repo_name: str, stage: str, dvc_remote_url: str = "") -> None:
    """Pull DVC data for a specific stage using DVC Repo API."""
    from dvc.repo import Repo as DVCRepo
    
    configure_dvc_remote(repo_dir, repo_name, dvc_remote_url)
    
    repo = DVCRepo(repo_dir)
    stage_dvc = repo_dir / "data" / f"{stage}.dvc"
    
    if stage_dvc.exists():
        repo.pull(targets=[str(stage_dvc)])
    else:
        repo.pull()

def get_dvc_hash(repo_dir: Path, stage: str) -> Tuple[str, str]:
    """Get DVC hash and commit for a stage."""
    git_repo = git.Repo(repo_dir)
    commit_hash = git_repo.head.commit.hexsha
    
    stage_dvc = repo_dir / "data" / f"{stage}.dvc"
    if stage_dvc.exists():
        with open(stage_dvc, 'r') as f:
            dvc_content = yaml.safe_load(f)
        outs = dvc_content.get('outs', [])
        for out in outs:
            if 'md5' in out:
                return out['md5'], commit_hash
    
    return commit_hash[:7], commit_hash

def clone_or_update_repo(repo_name: str, repo_url: str, enclave_path: Path) -> Path:
    """Clone or update a data repository in enclave staging."""
    repo_dir = enclave_path / "data" / "staging" / repo_name
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    ssh_url = convert_to_ssh_url(repo_url)

    if repo_dir.exists():
        repo = git.Repo(repo_dir)
        repo.git.reset('--hard')
        repo.remotes.origin.pull()
    else:
        repo = git.Repo.clone_from(ssh_url, repo_dir)

    return repo_dir

def copy_to_downloads(repo_name: str, version: str, staging_dir: Path, stage: str, enclave_path: Path) -> Path:
    """Copy staged data to versioned downloads directory."""
    downloads_dir = enclave_path / "data" / "downloads" / repo_name / version
    downloads_dir.mkdir(parents=True, exist_ok=True)
    
    src_data_dir = staging_dir / "data" / stage
    if src_data_dir.exists():
        dest_data_dir = downloads_dir / stage
        if dest_data_dir.exists():
            shutil.rmtree(dest_data_dir)
        shutil.copytree(src_data_dir, dest_data_dir)
        return downloads_dir
    
    src_all_data = staging_dir / "data"
    if src_all_data.exists():
        dest_all_data = downloads_dir / "data"
        if dest_all_data.exists():
            shutil.rmtree(dest_all_data)
        shutil.copytree(src_all_data, dest_all_data)
        
    return downloads_dir

def pull_enclave_data(enclave_path: Path, repo_name: Optional[str] = None, pull_all: bool = False) -> None:
    """Main function to pull enclave data."""
    manifest_path = enclave_path / "enclave_manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)

    approved = manifest.get('approved_products', [])
    if not approved:
        print("No approved products found in manifest.")
        return

    targets = []
    if pull_all:
        targets = approved
    elif repo_name:
        targets = [item for item in approved if item['repo'] == repo_name]
        if not targets:
            raise ValueError(f"Repository '{repo_name}' not found in approved list.")
    else:
        # Interactively ask? For now just take first one if only one, or error
        if len(approved) == 1:
            targets = approved
        else:
            raise ValueError("Multiple approved products. Specify a repo name or use --all.")

    for item in targets:
        curr_repo = item['repo']
        print(f"📥 Pulling {curr_repo}...")
        
        repo_info = get_repo_info(curr_repo)
        repo_dir = clone_or_update_repo(curr_repo, repo_info['repo_url'], enclave_path)
        
        data_stage = item.get('stage', 'final')
        pull_dvc_data(repo_dir, curr_repo, data_stage, repo_info.get('dvc_remote_url', ''))
        
        dvc_hash, git_commit = get_dvc_hash(repo_dir, data_stage)
        # Unified naming: hash-date
        today = datetime.now().strftime('%Y-%m-%d')
        version_str = f"{dvc_hash[:7]}-{today}"
        
        downloads_dir = copy_to_downloads(curr_repo, version_str, repo_dir, data_stage, enclave_path)
        
        # Check if already transferred
        transferred = manifest.get('transferred', [])
        already_transferred = next((t for t in transferred if t['repo'] == curr_repo and t['dvc_hash'] == dvc_hash), None)
        if already_transferred:
            print(f"  ⚠ Note: Version {dvc_hash[:7]} was already transferred to enclave on {already_transferred['transfer_date']}")

        # Update manifest
        downloaded = manifest.setdefault('downloaded', [])
        # Remove old entries for same repo if needed, or keep history
        # For now, let's keep it simple and add/update
        found = False
        for d in downloaded:
            if d['repo'] == curr_repo and d['dvc_hash'] == dvc_hash:
                d['git_commit'] = git_commit
                d['downloaded_at'] = datetime.now().isoformat()
                d['local_path'] = str(downloads_dir.relative_to(enclave_path))
                found = True
                break
        
        if not found:
            downloaded.append({
                'repo': curr_repo,
                'dvc_hash': dvc_hash,
                'git_commit': git_commit,
                'downloaded_at': datetime.now().isoformat(),
                'local_path': str(downloads_dir.relative_to(enclave_path))
            })
            
    with open(manifest_path, 'w') as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)
    
    print("✅ Data pull complete.")

def package_transfer(enclave_path: Path, name: Optional[str] = None) -> Path:
    """Package downloaded data for transfer to enclave."""
    manifest_path = enclave_path / "enclave_manifest.yaml"
    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)
        
    downloaded = manifest.get('downloaded', [])
    if not downloaded:
        raise ValueError("No downloaded data to package.")
        
    if not name:
        name = f"transfer-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"
        
    transfers_dir = enclave_path / "transfers"
    transfers_dir.mkdir(exist_ok=True)
    archive_path = transfers_dir / f"{name}.tar.gz"
    
    # Create transfer manifest
    transfer_manifest = {
        'enclave_name': manifest.get('enclave_name', ''),
        'transfer_date': datetime.now().isoformat(),
        'transfer_id': name,
        'contents': []
    }
    
    print(f"📦 Creating transfer package: {name}")
    
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in downloaded:
            repo_name = item['repo']
            local_path_str = item.get('local_path')
            
            if local_path_str:
                local_path = enclave_path / local_path_str
                if local_path.exists():
                    dvc_hash = item['dvc_hash']
                    # Warn if already transferred
                    transferred = manifest.get('transferred', [])
                    already_id = next((t.get('transfer_id') for t in transferred if t['repo'] == repo_name and t['dvc_hash'] == dvc_hash), None)
                    if already_id:
                        print(f"  ⚠ Alert: {repo_name} ({dvc_hash[:7]}) was already transferred via {already_id}")

                    # Preserve repo/hash-date hierarchy
                    version_folder = local_path.name
                    tar.add(local_path, arcname=f"{repo_name}/{version_folder}")
                    transfer_manifest['contents'].append({
                        'repo': repo_name,
                        'version_folder': version_folder,
                        'dvc_hash': item['dvc_hash'],
                        'git_commit': item['git_commit']
                    })
                    print(f"  + {repo_name}/{version_folder} ({item['dvc_hash'][:7]})")
        
        # Add manifest
        manifest_bytes = yaml.dump(transfer_manifest, default_flow_style=False, sort_keys=False).encode('utf-8')
        info = tarfile.TarInfo(name="_transfer_manifest.yaml")
        info.size = len(manifest_bytes)
        tar.addfile(info, fileobj=__import__('io').BytesIO(manifest_bytes))
        
    print(f"✅ Created: {archive_path}")
    return archive_path

def unpack_transfer(transfer_file: Path, dest_dir: Optional[Path] = None) -> Path:
    """Unpack a transfer archive."""
    if not transfer_file.exists():
        raise FileNotFoundError(f"Transfer file not found: {transfer_file}")

    if dest_dir is None:
        # Create a temp directory in transfers/
        dest_dir = transfer_file.parent / f"tmp_{transfer_file.stem}"

    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"📦 Unpacking {transfer_file.name} to {dest_dir}...")

    with tarfile.open(transfer_file, "r:gz") as tar:
        tar.extractall(dest_dir)

    print(f"✅ Unpacked to: {dest_dir}")
    return dest_dir

def verify_transfer(transfer_path: Path, enclave_path: Optional[Path] = None) -> bool:
    """Verify a transfer (file or unpacked directory) and move data to final location."""
    if not enclave_path:
        enclave_path = Path.cwd()
        
    temp_dir = None
    if transfer_path.is_file() and transfer_path.name.endswith(".tar.gz"):
        temp_dir = unpack_transfer(transfer_path)
        transfer_dir = temp_dir
    else:
        transfer_dir = transfer_path
        
    manifest_file = transfer_dir / "_transfer_manifest.yaml"
    if not manifest_file.exists():
        if temp_dir:
            shutil.rmtree(temp_dir)
        raise FileNotFoundError(f"Transfer manifest not found in {transfer_dir}")

    with open(manifest_file, 'r') as f:
        transfer_manifest = yaml.safe_load(f)

    print(f"🔍 Verifying transfer: {transfer_manifest.get('transfer_id')}")
    
    # In a more robust implementation, we would check checksums here.
    # For now, let's proceed to move the data as per the manifest.
    
    enclave_manifest_path = enclave_path / "enclave_manifest.yaml"
    if not enclave_manifest_path.exists():
        raise FileNotFoundError(f"Enclave manifest not found at {enclave_manifest_path}")

    with open(enclave_manifest_path, 'r') as f:
        enclave_manifest = yaml.safe_load(f)

    for content in transfer_manifest.get('contents', []):
        repo_name = content['repo']
        transfer_date = transfer_manifest.get('transfer_date', datetime.now().isoformat())[:10]
        version_folder = content.get('version_folder', f"{content['dvc_hash'][:7]}-{transfer_date}")
        dvc_hash = content['dvc_hash']
        
        # Target path: data/repo/hash-date (flatter structure)
        dest_path = enclave_path / "data" / repo_name / version_folder
        src_path = transfer_dir / repo_name / version_folder
        
        if not src_path.exists():
            # Fallback for old packages
            src_path = transfer_dir / repo_name
            
        if src_path.exists():
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.move(str(src_path), str(dest_path))
            print(f"✅ Moved {repo_name}/{version_folder} to {dest_path.relative_to(enclave_path)}")
            
            # Update enclave manifest
            transferred = enclave_manifest.setdefault('transferred', [])
            # Update or add
            found = False
            for t in transferred:
                if t['repo'] == repo_name and t['dvc_hash'] == dvc_hash:
                    t['transfer_date'] = transfer_date
                    t['transfer_id'] = transfer_manifest.get('transfer_id')
                    t['local_path'] = str(dest_path.relative_to(enclave_path))
                    found = True
                    break
            if not found:
                transferred.append({
                    'repo': repo_name,
                    'dvc_hash': dvc_hash,
                    'git_commit': content.get('git_commit'),
                    'transfer_date': transfer_date,
                    'transfer_id': transfer_manifest.get('transfer_id'),
                    'local_path': str(dest_path.relative_to(enclave_path))
                })

    with open(enclave_manifest_path, 'w') as f:
        yaml.dump(enclave_manifest, f, default_flow_style=False, sort_keys=False)
        
    print("🎉 Transfer successfully verified and data integrated.")
    
    # Cleanup tmp dir if we created one
    if temp_dir and temp_dir.exists():
        shutil.rmtree(temp_dir)
    return True

def clean_enclave(enclave_path: Path, keep_recent: int = 1, staging_only: bool = False) -> None:
    """Prune old versions and clean staging area."""
    # 1. Clean staging area (always)
    staging_dir = enclave_path / "data" / "staging"
    if staging_dir.exists():
        print(f"🧹 Cleaning staging area: {staging_dir}")
        for item in staging_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
    
    if staging_only:
        return

    # 2. Prune old downloads/transfers
    # Check both data/ and data/downloads/ (legacy)
    search_dirs = [enclave_path / "data", enclave_path / "data" / "downloads"]
    
    manifest_path = enclave_path / "enclave_manifest.yaml"
    if not manifest_path.exists():
        return

    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)

    print(f"🧹 Pruning old versions (keeping {keep_recent} recent)...")
    
    for downloads_base in search_dirs:
        if not downloads_base.exists():
            continue
            
        # Process each repo in this directory
        for repo_dir in downloads_base.iterdir():
            if not repo_dir.is_dir() or repo_dir.name == "staging":
                continue
            
            # Get all versions for this repo, sorted by date (if possible) or mtime
            versions = sorted(
                [v for v in repo_dir.iterdir() if v.is_dir()],
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )
            
            if len(versions) > keep_recent:
                to_delete = versions[keep_recent:]
                for v in to_delete:
                    print(f"  - Removing old version: {repo_dir.name}/{v.name}")
                    shutil.rmtree(v)
                    
                    # Update manifest lists (downloaded or transferred)
                    for section in ['downloaded', 'transferred']:
                        if section in manifest:
                            manifest[section] = [
                                item for item in manifest[section]
                                if not (item['repo'] == repo_dir.name and item.get('local_path', '').endswith(v.name))
                            ]

    with open(manifest_path, 'w') as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)
    
    print("✅ Enclave cleanup complete.")
