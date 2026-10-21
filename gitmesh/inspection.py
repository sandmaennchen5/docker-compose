"""Read-only repository inspection and explicit, race-checked reference operations."""
import os
from pathlib import Path
import tempfile


class StaleState(ValueError):
    pass


class RepositoryTools:
    def __init__(self, runner, fetch, data, redact):
        self.git, self.fetch, self.data, self.redact = runner, fetch, Path(data), redact

    def inventory(self, repo):
        path, snapshots, _, missing = self.fetch(repo, dry_run=True)
        return {'remotes': [{'name': remote, 'missing': remote in missing, 'count': len(refs),
                             'truncated': len(refs) > 1000,
                             'refs': [{'ref': ref, 'oid': oid} for ref, oid in sorted(refs.items())[:1000]]}
                            for remote, refs in snapshots.items()]}

    def history(self, path, revision):
        result = self.git(path, 'log', '--format=%H%x09%an%x09%aI%x09%s', '-n', '30', revision, '--', check=False)
        if result.returncode:
            return []
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split('\t', 3)
            if len(parts) == 4:
                commits.append(dict(zip(('oid', 'author', 'date', 'subject'), [self.redact(x) for x in parts])))
        return commits

    def describe_update(self, path, remote, ref, old, new):
        commits = self.history(path, (old + '..' + new) if old and ref.startswith('heads/') else new)
        return {'remote': remote, 'ref': ref, 'old': old, 'new': new, 'commits': commits,
                'commit_limit': 30, 'kind': 'update' if old else 'create'}

    def selection(self, snapshots, remote, ref):
        if remote not in snapshots or not isinstance(ref, str) or not ref.startswith(('heads/', 'tags/')):
            raise ValueError('Remote oder Referenz ungueltig')
        oid = snapshots[remote].get(ref)
        if not oid:
            raise ValueError('Referenz fehlt. Ansicht neu laden.')
        return oid

    def comparison(self, path, left, right):
        lcommit = self.git(path, 'rev-parse', '--verify', left + '^{commit}').stdout.strip()
        rcommit = self.git(path, 'rev-parse', '--verify', right + '^{commit}').stdout.strip()
        base = self.git(path, 'merge-base', lcommit, rcommit, check=False)
        counts = self.git(path, 'rev-list', '--left-right', '--count', lcommit + '...' + rcommit).stdout.split()
        stat = self.git(path, 'diff', '--no-ext-diff', '--no-textconv', '--stat', lcommit, rcommit, '--').stdout
        patch = self.git(path, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=3', lcommit, rcommit, '--').stdout
        return {'left_oid': left, 'right_oid': right, 'left_commit': lcommit, 'right_commit': rcommit,
                'base': base.stdout.strip() if base.returncode == 0 else None,
                'left_only': int(counts[0]), 'right_only': int(counts[1]),
                'left_commits': self.history(path, rcommit + '..' + lcommit),
                'right_commits': self.history(path, lcommit + '..' + rcommit),
                'stat': self.redact(stat), 'patch': self.redact(patch),
                'patch_truncated': len(patch) > 4000, 'commit_limit': 30}

    def compare(self, repo, body):
        path, snapshots, _, _ = self.fetch(repo, dry_run=True)
        ref = body.get('ref')
        left = self.selection(snapshots, body.get('left'), ref)
        right = self.selection(snapshots, body.get('right'), ref)
        return self.comparison(path, left, right)

    def change_ref(self, repo, body):
        path, snapshots, _, _ = self.fetch(repo, dry_run=True)
        remote, kind, name, action = body.get('remote'), body.get('kind'), body.get('name'), body.get('action')
        if remote not in snapshots or kind not in ('heads', 'tags') or action not in ('create', 'delete'):
            raise ValueError('Aktion, Remote oder Referenztyp ungueltig')
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError('Referenzname ungueltig')
        ref = kind + '/' + name
        if self.git(path, 'check-ref-format', 'refs/' + ref, check=False).returncode:
            raise ValueError('Git-Referenzname ungueltig')
        old = snapshots[remote].get(ref)
        if action == 'delete':
            if body.get('confirm_delete') is not True:
                raise ValueError('Loeschen muss ausdruecklich bestaetigt werden')
            if not old or body.get('expected_oid') != old:
                raise StaleState('Referenz wurde geaendert. Ansicht neu laden.')
            self.git(path, 'push', '--force-with-lease=refs/' + ref + ':' + old, remote, ':refs/' + ref)
            return {'status': 'deleted', 'remote': remote, 'ref': ref, 'old': old}
        if old:
            raise StaleState('Referenz existiert bereits. Ansicht neu laden.')
        source = self.selection(snapshots, body.get('from_remote'), body.get('from_ref'))
        commit = self.git(path, 'rev-parse', '--verify', source + '^{commit}').stdout.strip()
        self.git(path, 'push', '--force-with-lease=refs/' + ref + ':', remote, commit + ':refs/' + ref)
        return {'status': 'created', 'remote': remote, 'ref': ref, 'oid': commit}

    def merge(self, repo, body):
        path, snapshots, _, _ = self.fetch(repo, dry_run=True)
        ref, left_remote, right_remote, target = body.get('ref'), body.get('left'), body.get('right'), body.get('target')
        if not isinstance(ref, str) or not ref.startswith('heads/') or target not in (left_remote, right_remote):
            raise ValueError('Merge benoetigt einen Branch und eines der verglichenen Ziele')
        left = self.selection(snapshots, left_remote, ref)
        right = self.selection(snapshots, right_remote, ref)
        if body.get('expected_left') != left or body.get('expected_right') != right:
            raise StaleState('Branch wurde seit dem Vergleich geaendert. Erneut vergleichen.')
        if body.get('confirm_merge') is not True:
            raise ValueError('Merge muss ausdruecklich bestaetigt werden')
        common = self.git(path, 'merge-base', left, right, check=False)
        if common.returncode:
            return {'status': 'unrelated', 'message': 'Kein gemeinsamer Vorfahr. Historien lokal pruefen und zusammenfuehren.'}
        if self.git(path, 'merge-base', '--is-ancestor', right, left, check=False).returncode == 0:
            merged = left
        elif self.git(path, 'merge-base', '--is-ancestor', left, right, check=False).returncode == 0:
            merged = right
        else:
            # The temporary worktree is always generated directly inside the trusted data directory.
            with tempfile.TemporaryDirectory(prefix='.gitmesh-merge-', dir=self.data) as directory:
                worktree = Path(directory).resolve()
                if not worktree.is_relative_to(self.data.resolve()):
                    raise ValueError('Ungueltiges Arbeitsverzeichnis')
                self.git(path, 'worktree', 'add', '--detach', str(worktree), left)
                try:
                    identity = ('-c', 'user.name=GitMesh', '-c', 'user.email=gitmesh@localhost',
                                '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull)
                    result = self.git(worktree, *identity, 'merge', '--no-ff', '--no-commit', right, check=False)
                    if result.returncode:
                        conflicts = self.git(worktree, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                        if conflicts:
                            return {'status': 'conflict', 'files': conflicts[:200],
                                    'message': 'Inhaltskonflikte. Lokal aufloesen; kein Remote wurde veraendert.'}
                        raise ValueError('Merge lokal nicht moeglich; kein Remote wurde veraendert')
                    self.git(worktree, *identity, 'commit', '-m', 'GitMesh: merge ' + left_remote + ' and ' + right_remote)
                    merged = self.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                finally:
                    self.git(path, 'worktree', 'remove', '--force', str(worktree))
        expected = snapshots[target][ref]
        if expected != merged:
            self.git(path, 'push', '--force-with-lease=refs/' + ref + ':' + expected, target, merged + ':refs/' + ref)
        return {'status': 'merged', 'remote': target, 'ref': ref, 'oid': merged,
                'message': 'Ergebnis auf ein Ziel uebertragen. Weitere Ziele per Sync aktualisieren.'}
