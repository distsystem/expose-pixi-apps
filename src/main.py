#!/usr/bin/env python3
"""Clone pixi projects and expose executables via pixi's trampoline mechanism."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def get_clone_base_dir() -> Path:
    runner_temp = os.environ.get('RUNNER_TEMP', '/tmp')
    return Path(runner_temp) / 'expose-pixi-apps'


def parse_input() -> dict[str, object]:
    git_url = os.environ.get('INPUT_GIT', '')
    if not git_url:
        raise ValueError("'git' input is required")

    apps_raw = os.environ.get('INPUT_APPS', '')
    if not apps_raw:
        raise ValueError("'apps' input is required")
    apps = yaml.safe_load(apps_raw)
    if not isinstance(apps, list):
        raise ValueError("'apps' input must be a YAML list")

    entry: dict[str, object] = {'git': git_url, 'apps': apps}

    if ref := os.environ.get('INPUT_REF', ''):
        entry['ref'] = ref
    if env := os.environ.get('INPUT_ENVIRONMENT', ''):
        entry['environment'] = env

    exclude_raw = os.environ.get('INPUT_EXCLUDE_ENV_VARS', '')
    if exclude_raw:
        exclude = yaml.safe_load(exclude_raw)
        if isinstance(exclude, list):
            entry['exclude-env-vars'] = exclude

    return entry


def find_pixi() -> str:
    pixi = shutil.which('pixi')
    if not pixi:
        raise RuntimeError('pixi not found in PATH. Run setup-pixi before this action.')
    print(f'Found pixi at {pixi}')
    return pixi


def get_pixi_home() -> Path:
    return Path(os.environ.get('PIXI_HOME', Path.home() / '.pixi'))


def get_trampoline_bin() -> Path:
    return get_pixi_home() / 'bin' / 'trampoline_configuration' / 'trampoline_bin'


def ensure_trampoline_bin(pixi: str) -> None:
    if get_trampoline_bin().exists():
        return
    print('Trampoline binary not found, bootstrapping with `pixi global install coreutils`...')
    subprocess.run([pixi, 'global', 'install', 'coreutils'], check=True)


def clone_repo(entry: dict[str, object]) -> Path:
    git_url = str(entry['git'])
    repo_name = git_url.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
    clone_dir = get_clone_base_dir() / repo_name
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    git_args = ['--depth', '1']
    if ref := entry.get('ref'):
        git_args.extend(['--branch', str(ref)])

    ref_info = f' (ref: {ref})' if ref else ''
    print(f'Cloning {git_url}{ref_info} into {clone_dir}')

    cmd = ['git', 'clone'] + git_args + [git_url, str(clone_dir)]
    subprocess.run(cmd, check=True)
    return clone_dir


def pixi_install(pixi: str, clone_dir: Path, environment: str | None = None) -> None:
    cmd = [pixi, 'install', '--manifest-path', str(clone_dir / 'pixi.toml')]
    if environment and environment != 'default':
        cmd.extend(['-e', environment])
    print(f'Running pixi install in {clone_dir}...')
    subprocess.run(cmd, check=True, cwd=clone_dir)


def get_shell_hook(pixi: str, clone_dir: Path, environment: str | None = None) -> dict[str, object]:
    cmd = [pixi, 'shell-hook', '--json', '--manifest-path', str(clone_dir / 'pixi.toml')]
    if environment and environment != 'default':
        cmd.extend(['-e', environment])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=clone_dir,
        env={'PATH': os.environ.get('PATH', '')},
    )
    return json.loads(result.stdout)


_EXCLUDE_EXACT = {'PATH'}
_EXCLUDE_PREFIXES = ('PIXI_',)


def _should_exclude(key: str, extra: list[str] | None = None) -> bool:
    if key in _EXCLUDE_EXACT:
        return True
    if any(key.startswith(p) for p in _EXCLUDE_PREFIXES):
        return True
    return extra is not None and key in extra


def expose_entry(pixi: str, entry: dict[str, object]) -> None:
    clone_dir = clone_repo(entry)
    environment = str(entry.get('environment', 'default'))

    pixi_install(pixi, clone_dir, environment)

    print(f"Getting shell-hook for environment '{environment}'...")
    hook = get_shell_hook(pixi, clone_dir, environment)
    env_vars: dict[str, str] = hook['environment_variables']

    conda_prefix = env_vars.get('CONDA_PREFIX')
    if not conda_prefix:
        raise RuntimeError(f"CONDA_PREFIX not found in shell-hook output for environment '{environment}'")

    # Build path_diff (colon-separated string, matching pixi trampoline format)
    hook_path = env_vars.get('PATH', '')
    current_path = os.environ.get('PATH', '')
    new_path = hook_path[: len(hook_path) - len(current_path)] if hook_path.endswith(current_path) else hook_path
    path_diff = os.pathsep.join(p for p in new_path.split(os.pathsep) if p)

    # Filter env vars
    exclude_env_vars = entry.get('exclude-env-vars')
    filtered_env = {k: v for k, v in env_vars.items() if not _should_exclude(k, exclude_env_vars)}

    bin_dir = get_pixi_home() / 'bin'
    config_dir = bin_dir / 'trampoline_configuration'
    trampoline_bin = get_trampoline_bin()

    for app in entry['apps']:
        config_path = config_dir / f'{app}.json'
        exe_path = Path(conda_prefix) / 'bin' / app
        config = {'exe': str(exe_path), 'path_diff': path_diff, 'env': filtered_env}

        print(f"Writing trampoline config for '{app}' -> {exe_path}")
        config_path.write_text(json.dumps(config, indent=2))

        link_path = bin_dir / app
        link_path.unlink(missing_ok=True)
        os.link(trampoline_bin, link_path)
        print(f"Linked trampoline binary for '{app}' at {link_path}")


def main() -> None:
    if sys.platform == 'win32':
        print('::error::expose-pixi-apps is not supported on Windows')
        sys.exit(1)

    entry = parse_input()
    pixi = find_pixi()
    ensure_trampoline_bin(pixi)

    config_dir = get_pixi_home() / 'bin' / 'trampoline_configuration'
    config_dir.mkdir(parents=True, exist_ok=True)

    expose_entry(pixi, entry)


if __name__ == '__main__':
    main()
