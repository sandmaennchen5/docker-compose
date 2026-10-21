"""Read-only structured Git status for the web dashboard."""
import subprocess
from settings import git, load_settings


def overview(repo):
    state = git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=normal').stdout
    records = state.split('\0')
    files = []
    index = 0
    conflicts = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        code, name = record[:2], record[3:]
        previous = None
        if 'R' in code or 'C' in code:
            previous = records[index] if index < len(records) else None
            index += 1
        conflicted = code in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU')
        conflicts += int(conflicted)
        files.append(dict(path=name, previous=previous, code=code, conflict=conflicted))
    branch = git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip()
    head = git(repo, 'rev-parse', '--short', 'HEAD', check=False).stdout.strip()
    branches = git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').stdout.splitlines()
    tags = git(repo, 'tag', '--list').stdout.splitlines()
    history = git(repo, 'log', '-30', '--format=%h%x1f%an%x1f%aI%x1f%s%x1e', check=False)
    commits = []
    if history.returncode == 0:
        for entry in history.stdout.split('\x1e'):
            parts = entry.strip().split('\x1f', 3)
            if len(parts) == 4:
                commits.append(dict(commit=parts[0], author=parts[1], date=parts[2], subject=parts[3]))
    remotes = load_settings(repo)['remotes']
    for remote in remotes:
        remote.update(ahead=None, behind=None)
        if branch and head:
            counts = git(repo, 'rev-list', '--left-right', '--count',
                         'HEAD...refs/remotes/' + remote['name'] + '/' + branch, check=False)
            if counts.returncode == 0:
                values = counts.stdout.split()
                if len(values) == 2 and all(value.isdigit() for value in values):
                    remote.update(ahead=int(values[0]), behind=int(values[1]))
    try:
        version = subprocess.run(['multigit', 'version'], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        version = 'MultiGit lokal nicht verfuegbar'
    return dict(branch=branch or '(detached HEAD)', head=head, clean=not files,
                changed=len(files), conflicts=conflicts, files=files[:200],
                files_truncated=len(files) > 200, branches=branches, tags=tags,
                commits=commits, remotes=remotes, version=version)
