"""Execute the workflow's real shell in temporary repositories, with push stubbed."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/keepalive.yml'


def _git(repo, *args, env=None):
    return subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def _commit_script():
    script = WORKFLOW.read_text().split('      - name: Commit if changed\n        run: |\n', 1)[1]
    return '\n'.join(line[10:] for line in script.splitlines())


@pytest.mark.parametrize('existing,changed', [(False, True), (True, True), (True, False)])
def test_keepalive_first_file_changed_file_and_no_change(tmp_path, existing, changed):
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = {**os.environ, 'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1'}
    _git(repo, 'init', env=env)
    _git(repo, 'config', 'user.name', 'Test', env=env)
    _git(repo, 'config', 'user.email', 'test@example.invalid', env=env)
    (repo / 'README').write_text('test repo\n')
    path = repo / '.github/keepalive.txt'
    path.parent.mkdir()
    if existing:
        path.write_text('same timestamp\n')
    _git(repo, 'add', '.', env=env)
    _git(repo, 'commit', '-m', 'baseline', env=env)
    baseline = _git(repo, 'rev-parse', 'HEAD', env=env).strip()
    path.write_text('new timestamp\n' if changed else 'same timestamp\n')
    # The workflow must not accidentally commit another staged path.
    unrelated = repo / 'unrelated'
    unrelated.write_text('do not commit\n')
    _git(repo, 'add', 'unrelated', env=env)
    real_git = shutil.which('git')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stub = bin_dir / 'git'
    stub.write_text(f'#!/bin/sh\nif [ "$1" = push ]; then\n  echo push >> "$PUSH_LOG"\n  exit 0\nfi\nexec "{real_git}" "$@"\n')
    stub.chmod(0o755)
    push_log = tmp_path / 'push.log'
    env.update(PATH=f'{bin_dir}:{env["PATH"]}', PUSH_LOG=str(push_log))
    result = subprocess.run(['bash', '-e', '-c', _commit_script()], cwd=repo, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    head = _git(repo, 'rev-parse', 'HEAD', env=env).strip()
    assert (head != baseline) == changed
    assert push_log.exists() == changed
    if changed:
        assert _git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD', env=env).strip() == '.github/keepalive.txt'
    else:
        assert 'No change, skipping commit' in result.stdout
    assert _git(repo, 'diff', '--cached', '--name-only', env=env).strip() == 'unrelated'
