"""GitMesh: conservative multi-remote Git synchronization, Python standard library only."""
from contextlib import contextmanager
import hashlib
import base64
import fnmatch
import math
import signal
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote, quote_plus
import management
import inspection
import runtime
import transport
import releases
import metadata
import authorship
import conflicts as conflict_store
import sys

DATA = Path(os.environ.get('GITMESH_DATA', '/data'))
CONFIG = Path(os.environ.get('GITMESH_CONFIG', str(DATA / 'gitmesh.json')))
LOCK = threading.Lock()
WORKER = None
MAINTENANCE = False
GIT_CONTEXT = threading.local()


def control():
    return management.Control(DATA)


def redact_git_output(output):
    text = output.decode('utf-8', errors='replace') if isinstance(output, bytes) else (output or '')
    secrets = []
    for username, token in getattr(GIT_CONTEXT, 'credentials', {}).values():
        if token:
            secrets.extend([token, quote(token, safe=''), quote_plus(token),
                            base64.b64encode(token.encode()).decode(),
                            base64.b64encode((username + ':' + token).encode()).decode()])
    secrets.extend(value for key, value in os.environ.items() if key.startswith('GITMESH_') and any(part in key for part in ('TOKEN', 'SECRET', 'KEY')) and value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(https?://)[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?im)(authorization|private-token|x-gitlab-token)\s*[:=][^\r\n]*', r'\1: [REDACTED]', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)
    return text.strip()[:4000]


class GitFailure(RuntimeError):
    def __init__(self, operation, remote=None, exit_code=None, stderr=''):
        super().__init__('Git operation failed: ' + operation)
        self.retryable = operation != 'push'
        error = redact_git_output(stderr)
        hints = [
            (('host key verification failed',), 'SSH-Hostschluessel in known_hosts pruefen.'),
            (('permission denied (publickey)',), 'SSH-Schluessel, Benutzer und Repository-Berechtigung pruefen.'),
            (('authentication failed', 'could not read username', 'terminal prompts disabled'), 'HTTPS-Provider zuordnen und Token sowie Git-Benutzername pruefen.'),
            (('repository not found', 'does not appear to be a git repository'), 'Repository-URL und Leserechte pruefen; Ziel-Repositories muessen bereits existieren.'),
            (('could not resolve host', 'could not resolve hostname'), 'DNS und Hostnamen aus dem Container pruefen.'),
            (('ssl certificate problem', 'certificate verify failed'), 'Zertifikatskette und CA-Vertrauen im Container pruefen.'),
            (('non-fast-forward', 'stale info', 'protected branch', 'pre-receive hook declined'), 'Zielbranch wurde geaendert oder durch Branchschutz abgelehnt.'),
            (('timed out', 'timeout'), 'Netzwerkverbindung und Erreichbarkeit des Git-Servers pruefen.')]
        hint = next((hint for patterns, hint in hints if any(pattern in error.lower() for pattern in patterns)),
                    'Git-Ausgabe sowie URL, Verbindung und Berechtigungen pruefen.')
        self.details = {'message': 'Git-' + operation + ' fehlgeschlagen' + (' (Remote: ' + remote + ')' if remote else ''),
                        'operation': operation, 'remote': remote, 'exit_code': exit_code, 'git_error': error, 'hint': hint}


def _git(path, *args, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, LC_ALL='C')
    env.update(getattr(GIT_CONTEXT, 'ssh_env', {}))
    credentials = getattr(GIT_CONTEXT, 'credentials', {})
    remote = next((arg for arg in args[1:] if arg in credentials), None) if args[0] in ('fetch', 'push', 'ls-remote', 'lfs') else None
    if remote:
        username, token = credentials[remote]
        env.update(GIT_ASKPASS=str(Path(__file__).with_name('askpass.py')), GITMESH_GIT_USERNAME=username,
                   GITMESH_GIT_TOKEN=token)
    remote_name = next((arg for arg in args[1:] if not arg.startswith('-')), None) if args[0] in ('fetch', 'push', 'ls-remote', 'lfs') else None
    try:
        result = subprocess.run(['git', '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                             '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
                             '-C', str(path), *args], capture_output=True, text=True, timeout=180, env=env)
    except subprocess.TimeoutExpired as exc:
        raise GitFailure(args[0], remote_name, None, 'Git timeout after 180 seconds.\n' + redact_git_output(exc.stderr)) from None
    except OSError:
        raise GitFailure(args[0], remote_name, None, 'Git executable could not be started. Check installation and file permissions.') from None
    if check and result.returncode:
        raise GitFailure(args[0], remote_name, result.returncode, result.stderr)
    return result


def git(path, *args, check=True):
    repo = getattr(GIT_CONTEXT, 'repo', None)
    remote = next((arg for arg in args[1:] if repo and arg in repo['remotes']), None) if args and args[0] in ('fetch', 'push', 'ls-remote', 'lfs') else None
    if repo and remote:
        with transport.ssh_environment(control(), repo, remote) as ssh_env:
            GIT_CONTEXT.ssh_env = ssh_env
            try:
                return _git(path, *args, check=check)
            finally:
                GIT_CONTEXT.ssh_env = {}
    return _git(path, *args, check=check)


def _validate_config(config):
    if not isinstance(config, dict) or set(config) - {'repositories', 'interval', 'worker', 'public_url', 'worker_sets', 'profiles'}:
        raise ValueError('Invalid configuration fields')
    public = config.get('public_url', '')
    if not isinstance(public, str):
        raise ValueError('Invalid public URL')
    if public:
        parsed = urlsplit(public)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or any(c.isspace() for c in public):
            raise ValueError('Public URL requires HTTPS without credentials')
        parsed.port
    interval = config.get('interval', 300)
    if type(interval) is not int or not 30 <= interval <= 86400:
        raise ValueError('Intervall muss zwischen 30 und 86400 Sekunden liegen')
    repos = config.get('repositories')
    if not isinstance(repos, list) or len(repos) > 200:
        raise ValueError('Maximal 200 Repository-Netze erlaubt')
    worker = config.get('worker', {})
    if not isinstance(worker, dict) or set(worker) - {'parallelism', 'min_free_mb', 'min_memory_mb', 'max_load'}:
        raise ValueError('Ungültige Worker-Einstellungen')
    for key, low, high, default in [('parallelism', 1, 8, 2), ('min_free_mb', 0, 1000000, 128),
                                    ('min_memory_mb', 0, 1000000, 0)]:
        value = worker.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Ungültiges Ressourcenlimit: ' + key)
    max_load = worker.get('max_load', 0)
    if type(max_load) not in (int, float) or not math.isfinite(max_load) or not 0 <= max_load <= 1024:
        raise ValueError('Invalid CPU load limit')
    names = set()
    claimed = set()
    for repo in repos:
        if not isinstance(repo, dict) or set(repo) - {'name', 'enabled', 'remotes', 'webhook_secret_env', 'direction', 'source', 'policy', 'branches', 'tags', 'interval', 'remote_providers', 'force_confirmed', 'create_missing', 'create_private', 'auto_webhooks', 'worker_set', 'profile', 'releases', 'release_assets', 'release_max_asset_mb', 'sync_branches', 'sync_overrides', *metadata.OPTIONS}:
            raise ValueError('Invalid repository fields')
        name = repo.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name) or name in names:
            raise ValueError('Netznamen müssen eindeutig sein und mit einem Buchstaben oder einer Ziffer beginnen')
        names.add(name)
        if type(repo.get('enabled', True)) is not bool:
            raise ValueError('Aktiv muss ein Wahrheitswert sein')
        remotes = repo.get('remotes')
        if not isinstance(remotes, dict) or not 2 <= len(remotes) <= 20:
            raise ValueError('Pro Netz sind 2 bis 20 Remotes erforderlich')
        direction, policy = repo.get('direction', 'bidirectional'), repo.get('policy', 'ff-only')
        if direction not in ('bidirectional', 'one-way') or policy not in ('ff-only', 'reject', 'force-with-lease'):
            raise ValueError('Ungültige Sync-Richtung oder Konfliktregel')
        if direction == 'one-way' and repo.get('source') not in remotes:
            raise ValueError('Quell-Remote für Einweg-Sync fehlt')
        if policy == 'force-with-lease' and (direction != 'one-way' or repo.get('force_confirmed') is not True):
            raise ValueError('Erzwungene Updates benötigen Einwegmodus und explizite Bestätigung')
        branches = repo.get('branches', [])
        if not isinstance(branches, list) or len(branches) > 50 or any(not isinstance(x, str) or not 1 <= len(x) <= 200 for x in branches):
            raise ValueError('Ungültiger Branch-Filter')
        if type(repo.get('sync_branches', True)) is not bool:
            raise ValueError('Ungueltige Branch-Auswahl')
        for option in metadata.OPTIONS:
            if type(repo.get(option, False)) is not bool:
                raise ValueError('Ungueltige Sync-Auswahl: ' + option)
        if repo.get('comments') and not (repo.get('issues') or repo.get('pull_requests')):
            raise ValueError('Kommentare benoetigen Issues oder Pull Requests')
        if any(repo.get(key) for key in metadata.OPTIONS if key != 'lfs') and (repo.get('source') not in remotes or any(not repo.get('remote_providers', {}).get(remote) for remote in remotes)):
            raise ValueError('Metadaten/Wiki benoetigen Quelle und Provider fuer alle Remotes')
        if type(repo.get('tags', True)) is not bool:
            raise ValueError('Ungültige Tag-Einstellung')
        if 'interval' in repo and (type(repo['interval']) is not int or repo['interval'] != 0 and not 30 <= repo['interval'] <= 86400):
            raise ValueError('Profilintervall: 0 (manuell) oder 30 bis 86400 Sekunden')
        providers = repo.get('remote_providers', {})
        if not isinstance(providers, dict) or set(providers) - set(remotes) or any(not isinstance(x, str) for x in providers.values()):
            raise ValueError('Ungültige Provider-Zuordnung')
        for option in ('create_missing', 'create_private', 'auto_webhooks', 'releases', 'release_assets'):
            if type(repo.get(option, option in ('create_private', 'release_assets'))) is not bool:
                raise ValueError('Invalid automatic repository creation setting')
        if type(repo.get('release_max_asset_mb', 512)) is not int or not 1 <= repo.get('release_max_asset_mb', 512) <= 4096:
            raise ValueError('Release-Anhanglimit: 1 bis 4096 MB')
        if repo.get('releases') and (repo.get('source') not in remotes or not repo.get('tags', True) or any(not providers.get(remote) for remote in remotes)):
            raise ValueError('Release-Sync benoetigt Quelle, aktivierte Tags und Provider fuer alle Remotes')
        if repo.get('auto_webhooks') and (not public or any(not providers.get(remote) for remote in remotes)):
            raise ValueError('Automatic webhooks require public URL and all provider mappings')
        if repo.get('create_missing'):
            if repo.get('source') not in remotes:
                raise ValueError('Select the source remote before enabling automatic creation')
            if any(not providers.get(remote) for remote in remotes if remote != repo['source']):
                raise ValueError('Automatic creation requires a provider for every target remote')
        profile_urls = set()
        secret = repo.get('webhook_secret_env', '')
        if not isinstance(secret, str) or (secret and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', secret)):
            raise ValueError('Ungültiger Name der Secret-Umgebungsvariable')
        for remote, url in remotes.items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', remote):
                raise ValueError('Ungültiger Remote-Name')
            if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
                raise ValueError('Ungültige Remote-URL')
            parsed = urlsplit(url)
            if parsed.scheme not in ('https', 'http', 'ssh') or not parsed.hostname or parsed.password or (parsed.scheme in ('https', 'http') and parsed.username):
                raise ValueError('HTTPS oder SSH ohne eingebettete Zugangsdaten verwenden')
            parsed.port
            if parsed.scheme == 'http' and not providers.get(remote):
                raise ValueError('HTTP-Remote benoetigt einen zugeordneten Provider mit ausdruecklicher HTTP-Freigabe')
            if parsed.query or parsed.fragment:
                raise ValueError('Remote-URLs ohne Query oder Fragment verwenden')
            canonical = (parsed.hostname.lower(), parsed.path.rstrip('/').removesuffix('.git'))
            if canonical in claimed or canonical in profile_urls:
                raise ValueError('Repository ist bereits einem anderen Remote oder Profil zugeordnet')
            profile_urls.add(canonical)
        claimed.update(profile_urls)
    return config


COMPONENT_FIELDS = {'sync_branches', 'tags', 'releases', 'release_assets', 'release_max_asset_mb', *metadata.OPTIONS}


SHARED_FIELDS = {'enabled', 'direction', 'source', 'policy', 'branches', 'tags', 'interval', 'worker_set', 'force_confirmed', 'create_missing', 'create_private', 'auto_webhooks', 'webhook_secret_env', 'releases', 'release_assets', 'release_max_asset_mb', 'sync_branches', *metadata.OPTIONS}


def resolve_config(config):
    result = dict(config)
    profiles = config.get('profiles', {})
    result['repositories'] = []
    for repo in config.get('repositories', []):
        identifier = repo.get('profile', '')
        if identifier and identifier not in profiles:
            raise ValueError('Gemeinsames Profil nicht gefunden')
        overrides = repo.get('sync_overrides', {})
        if not isinstance(overrides, dict) or set(overrides) - COMPONENT_FIELDS:
            raise ValueError('Ungueltige lokale Sync-Auswahl')
        # Shared timing/policy stays authoritative; explicit component choices are local.
        effective = dict(repo, **profiles.get(identifier, {}))
        effective.update(overrides)
        result['repositories'].append(effective)
    return result


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('Configuration object required')
    profiles, groups = config.get('profiles', {}), config.get('worker_sets', {})
    if not isinstance(profiles, dict) or len(profiles) > 100 or not isinstance(groups, dict) or len(groups) > 16:
        raise ValueError('Maximal 100 Profile und 16 Worker-Gruppen')
    for name, profile in profiles.items():
        if not isinstance(name, str) or not management.IDENTIFIER.fullmatch(name) or not isinstance(profile, dict) or set(profile) - SHARED_FIELDS:
            raise ValueError('Ungueltiges gemeinsames Profil')
    for name, limits in groups.items():
        if not isinstance(name, str) or not management.IDENTIFIER.fullmatch(name) or name == 'default':
            raise ValueError('Ungueltige Worker-Gruppe')
        _validate_config({'repositories': [], 'worker': limits})
    _validate_config(resolve_config(config))
    for repo in resolve_config(config)['repositories']:
        if repo.get('worker_set', 'default') != 'default' and repo['worker_set'] not in groups:
            raise ValueError('Worker-Gruppe nicht gefunden')
    return config


def load_config(expand=True):
    config = validate_config(json.loads(CONFIG.read_text(encoding='utf-8')))
    return resolve_config(config) if expand else config


def _save_config(config):
    validate_config(config)
    if not LOCK.acquire(blocking=False):
        return False
    temporary = None
    try:
        if MAINTENANCE:
            return False
        for repo in resolve_config(config)['repositories']:
            for remote, identifier in repo.get('remote_providers', {}).items():
                provider = control().provider(identifier)
                url = urlsplit(repo['remotes'][remote])
                if repo.get('create_missing') and remote != repo['source']:
                    management.Control.repository_path(provider, repo['remotes'][remote])
                if url.scheme == 'http' and not provider.get('allow_http'):
                    raise ValueError('Internes HTTP ist fuer diesen Provider nicht freigegeben')
                if url.scheme in ('https', 'http') and (url.scheme, url.netloc) != (urlsplit(provider['url']).scheme, urlsplit(provider['url']).netloc):
                    raise ValueError('Remote-URL passt nicht zum zugeordneten Provider')
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CONFIG.parent,
                                         prefix='.gitmesh-', delete=False) as handle:
            temporary = handle.name
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG)
        temporary = None
        return True
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        LOCK.release()


def save_config(config):
    try:
        with runtime.file_lock(DATA, 'configuration'):
            return _save_config(config)
    except runtime.Busy:
        return False


@contextmanager
def database():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'history.db', timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, time REAL, repository TEXT, status TEXT, details TEXT)')
    try:
        with db:
            yield db
    finally:
        db.close()


def fetch_snapshots(repo, dry_run=False):
    path = DATA / repo['name']
    path.mkdir(parents=True, exist_ok=True)
    if not (path / 'HEAD').exists():
        git(path, 'init', '--bare')
    snapshots = {}
    created = []
    missing = []
    remotes = list(repo['remotes'].items())
    if repo.get('create_missing'):
        if repo.get('source') not in repo['remotes']:
            raise ValueError('Automatic creation requires a source remote')
        # Fetch the source successfully before making any provider-side changes.
        remotes.sort(key=lambda item: item[0] != repo['source'])
    for remote, url in remotes:
        original_url = url
        url = transport.remote_url(control(), repo, remote) if repo.get('remote_providers', {}).get(remote) else url
        if repo.get('create_missing') and remote != repo['source']:
            identifier = repo.get('remote_providers', {}).get(remote)
            if not identifier:
                raise ValueError('Automatic creation requires a target provider')
            if dry_run and not control().repository_exists(identifier, original_url):
                snapshots[remote] = {}
                missing.append(remote)
                continue
            if not dry_run and control().ensure_repository(identifier, original_url, repo.get('create_private', True)):
                created.append(remote)
                if hasattr(GIT_CONTEXT, 'created'):
                    GIT_CONTEXT.created.append(remote)
        if git(path, 'remote', 'get-url', remote, check=False).returncode:
            git(path, 'remote', 'add', remote, url)
        else:
            git(path, 'remote', 'set-url', remote, url)
        git(path, 'fetch', '--prune', '--no-tags', remote,
            f'+refs/heads/*:refs/mesh/{remote}/heads/*',
            f'+refs/tags/*:refs/mesh/{remote}/tags/*')
        refs = git(path, 'for-each-ref', '--format=%(refname) %(objectname)', f'refs/mesh/{remote}/').stdout
        prefix = f'refs/mesh/{remote}/'
        snapshots[remote] = {line.split()[0][len(prefix):]: line.split()[1] for line in refs.splitlines()}
    return path, snapshots, created, missing


def repository_tools():
    return inspection.RepositoryTools(git, fetch_snapshots, DATA, redact_git_output)


def source_default_branch(repo, path, snapshots):
    source = repo.get('source')
    if source not in snapshots or not any(ref.startswith('heads/') for ref in snapshots[source]):
        source = next((remote for remote in repo['remotes'] if any(ref.startswith('heads/') for ref in snapshots[remote])), None)
    if not source:
        return None
    advertised = git(path, 'ls-remote', '--symref', source, 'HEAD').stdout
    return next((parts[1].removeprefix('refs/') for line in advertised.splitlines()
                 if len(parts := line.split()) == 3 and parts[0] == 'ref:' and parts[2] == 'HEAD'
                 and parts[1].startswith('refs/heads/') and parts[1].removeprefix('refs/') in snapshots[source]), None)


def prioritize_initial_default(repo, path, snapshots, plan, default=None):
    empty = {remote for remote, refs in snapshots.items() if not any(ref.startswith('heads/') for ref in refs)}
    if not empty or not any(remote in empty and ref.startswith('heads/') for remote, ref, _, _ in plan):
        return plan
    default = default or source_default_branch(repo, path, snapshots)
    if not default:
        return plan
    return sorted(plan, key=lambda item: 0 if item[0] in empty and item[1] == default else 1)


def selected_components(repo):
    return [key for key in ('sync_branches', 'tags', 'releases', *metadata.OPTIONS) if repo.get(key, key in ('sync_branches', 'tags'))]


def sync_lfs(repo, path, snapshots):
    # Transfer objects before publishing refs; keep filters and unrelated target refs intact.
    git(path, 'lfs', 'version')
    sources = [repo['source']] if repo.get('direction') == 'one-way' else list(repo['remotes'])
    refs = []
    for remote in sources:
        selected = [f'refs/mesh/{remote}/{ref}' for ref in snapshots[remote]
                    if (ref.startswith('heads/') and repo.get('sync_branches', True)
                        and (not repo.get('branches') or any(fnmatch.fnmatchcase(ref[6:], pattern) for pattern in repo['branches'])))
                    or (ref.startswith('tags/') and repo.get('tags', True))]
        if selected:
            git(path, 'lfs', 'fetch', '--all', remote, *selected)
            refs.extend(selected)
    if refs:
        for remote in repo['remotes']:
            if repo.get('direction') != 'one-way' or remote != repo['source']:
                git(path, 'lfs', 'push', '--all', remote, *refs)


def sync_wiki(repo):
    # Nested cache retains the same remote names for existing credential/SSH scoping.
    wiki = dict(repo, name=repo['name'] + '/wiki.git', direction='one-way', policy='ff-only',
                sync_branches=True, branches=[], tags=False, releases=False, create_missing=False, _wiki_cache=True,
                auto_webhooks=False, **{key: False for key in metadata.OPTIONS})
    wiki['remotes'] = {name: url.removesuffix('.git') + '.wiki.git' for name, url in repo['remotes'].items()}
    status, details = synchronize(wiki)
    return dict(details, status=status)


def synchronize(repo, dry_run=False):
    path, snapshots, created, missing = fetch_snapshots(repo, dry_run)
    actions, conflicts, plan = [], [], []
    direction, policy = repo.get('direction', 'bidirectional'), repo.get('policy', 'ff-only')
    source = repo.get('source')
    references = set(snapshots[source]) if direction == 'one-way' else set().union(*(set(s) for s in snapshots.values()))
    for ref in sorted(references):
        if ref.startswith('heads/') and not repo.get('sync_branches', True):
            continue
        if ref.startswith('heads/') and repo.get('branches') and not any(fnmatch.fnmatchcase(ref[6:], pattern) for pattern in repo['branches']):
            continue
        if ref.startswith('tags/') and not repo.get('tags', True):
            continue
        tips = {snapshot[ref] for snapshot in snapshots.values() if ref in snapshot}
        if direction == 'one-way':
            winner = snapshots[source][ref]
        elif len(tips) == 1:
            winner = next(iter(tips))
        elif ref.startswith('tags/'):
            conflicts.append(ref)
            continue
        else:
            winners = [tip for tip in tips if all(other == tip or
                       git(path, 'merge-base', '--is-ancestor', other, tip, check=False).returncode == 0 for other in tips)]
            if len(winners) != 1:
                conflicts.append(ref)
                continue
            winner = winners[0]
        for remote, snapshot in snapshots.items():
            if direction == 'one-way' and remote == source or snapshot.get(ref) == winner:
                continue
            old = snapshot.get(ref)
            compatible = not old or (ref.startswith('heads/') and
                         git(path, 'merge-base', '--is-ancestor', old, winner, check=False).returncode == 0)
            if not compatible and policy != 'force-with-lease':
                conflicts.append(ref + ' @ ' + remote)
                continue
            plan.append((remote, ref, winner, old))
    initial = {remote for remote, refs in snapshots.items() if remote != source and repo.get('remote_providers', {}).get(remote) and not repo.get('_wiki_cache') and (remote in created or not any(ref.startswith('heads/') for ref in refs)) and any(item[0] == remote and item[1].startswith('heads/') for item in plan)}
    default = source_default_branch(repo, path, snapshots) if initial else None
    plan = prioritize_initial_default(repo, path, snapshots, plan, default)
    if dry_run:
        return ('conflict' if conflicts else 'success'), {'plan': [repository_tools().describe_update(path, remote, ref, old, winner) for remote, ref, winner, old in plan[:200]], 'plan_truncated': len(plan) > 200, 'conflicts': conflicts, 'blocked': bool(conflicts and policy == 'reject'), 'missing_repositories': missing, 'selected_components': selected_components(repo), 'note': 'Vorschau plant Git-Referenzen; Metadaten, Wiki und LFS werden erst beim echten Sync verarbeitet.'}
    hooks = []
    if repo.get('auto_webhooks'):
        hooks = control().register_hooks(repo, load_config().get('public_url', ''), os.environ.get(repo.get('webhook_secret_env', ''), ''), refresh_secret=False)
    if conflicts and policy == 'reject':
        return 'conflict', {'updated': [], 'conflicts': conflicts, 'created_repositories': created, 'message': 'Gesamtes Profil wegen Konflikt übersprungen'}
    default_errors, default_warnings, blocked_initial = [], [], set()
    for remote in sorted(initial):
        if default and any(item[0] == remote and item[1] == default for item in plan):
            try:
                control().queue_initial_default(repo, remote, default.removeprefix('heads/'))
            except (management.ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
                blocked_initial.add(remote)
                default_errors.append({'remote': remote, 'message': str(exc) if isinstance(exc, management.ProviderError) else 'Initialer Default-Branch konnte nicht vorgemerkt werden'})
        else:
            default_warnings.append({'remote': remote, 'branch': default.removeprefix('heads/') if default else None, 'message': 'Quell-Default nicht ermittelbar oder durch Branch-Auswahl ausgeschlossen'})
    if repo.get('lfs'):
        sync_lfs(repo, path, snapshots)
    for remote, ref, winner, old in plan:
        if remote in blocked_initial:
            continue
        args = ['push']
        if policy == 'force-with-lease':
            args.append(f'--force-with-lease=refs/{ref}:{old or ""}')
        git(path, *args, remote, f'{winner}:refs/{ref}')
        snapshots[remote][ref] = winner
        actions.append(f'{remote}: {ref} -> {winner[:12]}')
    details = {'updated': actions, 'conflicts': conflicts, 'created_repositories': created, 'webhooks': hooks, 'selected_components': selected_components(repo)}
    if repo.get('remote_providers') and not repo.get('_wiki_cache'):
        details['default_branch'] = control().apply_initial_defaults(repo, snapshots)
        details['default_branch']['errors'].extend(default_errors)
        details['default_branch']['warnings'].extend(default_warnings)
    if repo.get('lfs'):
        details['lfs'] = 'success'
    if any(repo.get(key) for key in metadata.OPTIONS if key not in ('wiki', 'lfs')):
        details['metadata'] = metadata.MetadataSync(control()).sync(repo, snapshots)
    if repo.get('wiki'):
        try:
            details['wiki'] = sync_wiki(repo)
        except GitFailure as exc:
            details['wiki'] = {'status': 'error', **exc.details}
    if repo.get('releases'):
        try:
            details['releases'] = releases.ReleaseSync(control()).sync(repo, snapshots)
        except (management.ProviderError, ValueError, TypeError, KeyError, AttributeError) as exc:
            details['releases'] = {'errors': [{'message': str(exc) if isinstance(exc, management.ProviderError) else 'Release-Sync fehlgeschlagen'}]}
    if details.get('default_branch', {}).get('errors') or details.get('releases', {}).get('errors') or details.get('metadata', {}).get('errors') or details.get('wiki', {}).get('status') == 'error':
        return 'error', details
    if (details.get('releases', {}).get('conflicts') or any(asset['status'] == 'conflict' for asset in details.get('releases', {}).get('assets', []))
            or details.get('metadata', {}).get('counts', {}).get('conflict') or details.get('wiki', {}).get('status') == 'conflict'):
        return 'conflict', details
    return ('conflict' if conflicts else 'success'), details


def prepare_credentials(repo):
    credentials = {}
    for remote, identifier in repo.get('remote_providers', {}).items():
        provider = control().provider(identifier, reveal=True)
        parsed = urlsplit(transport.remote_url(control(), repo, remote))
        if parsed.scheme in ('https', 'http'):
            if parsed.scheme == 'http' and not provider.get('allow_http'):
                raise ValueError('Internes HTTP ist fuer diesen Provider nicht freigegeben')
            if (parsed.scheme, parsed.netloc) != (urlsplit(provider['url']).scheme, urlsplit(provider['url']).netloc):
                raise ValueError('Remote-URL und Provider stimmen nicht überein')
            if provider['token']:
                username = provider['username'] or ('oauth2' if provider['kind'] == 'gitlab' else 'x-access-token')
                credentials[remote] = (username, provider['token'])
    return credentials


def execute_profile(repo):
    GIT_CONTEXT.repo = repo
    GIT_CONTEXT.credentials = prepare_credentials(repo)
    GIT_CONTEXT.created = []
    try:
        status, details = synchronize(repo)
        with database() as db:
            db.execute('INSERT INTO runs(time, repository, status, details) VALUES(?,?,?,?)',
                       (time.time(), repo['name'], status, json.dumps(details)))
        return status, details
    except Exception as exc:
        if GIT_CONTEXT.created:
            details = getattr(exc, 'details', {'message': 'Sync failed after repository creation'})
            details['created_repositories'] = list(GIT_CONTEXT.created)
            exc.details = details
        raise
    finally:
        GIT_CONTEXT.credentials = {}
        GIT_CONTEXT.created = []
        GIT_CONTEXT.repo = None



def run_all(name=None, origin='manual'):
    names = [r['name'] for r in load_config()['repositories'] if r.get('enabled', True) and (not name or r['name'] == name)]
    return control().enqueue(names, origin)


def _repository_action(action, body, actor):
    global MAINTENANCE
    with LOCK:
        if MAINTENANCE or WORKER and WORKER.active:
            raise inspection.StaleState('Aktive Operation zuerst abschliessen lassen')
        MAINTENANCE = True
    try:
        config = load_config()
        repo = next((r for r in config['repositories'] if r['name'] == body.get('profile')), None)
        if repo is None:
            raise ValueError('Profil nicht gefunden')
        GIT_CONTEXT.repo = repo
        GIT_CONTEXT.credentials = prepare_credentials(repo)
        tools = repository_tools()
        if action == 'preview':
            status, result = synchronize(repo, dry_run=True)
            return dict(result, status=status)
        if action == 'refs':
            return tools.inventory(repo)
        if action == 'compare':
            return tools.compare(repo, body)
        if action == 'refs/change':
            result = tools.change_ref(repo, body)
        elif action == 'merge':
            result = tools.merge(repo, body)
        elif action == 'conflicts/resolve':
            result = conflict_store.resolve(control(), repo, body, actor['name'])
        elif action == 'webhooks/register':
            result = {'webhooks': control().register_hooks(repo, config.get('public_url', ''), os.environ.get(repo.get('webhook_secret_env', ''), ''))}
        else:
            raise ValueError('Unknown operation')
        control().record_action(repo['name'], action, result, actor['name'], 'conflict' if result.get('status') in ('conflict', 'unrelated') else 'error' if result.get('status') == 'error' else 'success')
        return result
    finally:
        GIT_CONTEXT.credentials = {}
        GIT_CONTEXT.repo = None
        with LOCK:
            MAINTENANCE = False


def repository_action(action, body, actor):
    with runtime.file_lock(DATA):
        return _repository_action(action, body, actor)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass

    def reply(self, code, body, content_type='application/json'):
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def token(self):
        header = self.headers.get('Authorization', '')
        return header[7:] if header.startswith('Bearer ') else ''

    def actor(self):
        return control().actor(self.token())

    def authorized(self):
        return self.actor() is not None

    def do_GET(self):
        try:
            self.get()
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error': 'Ungültige Anfrage'})
        except management.RateLimited as exc:
            self.reply(429, {'error': str(exc)})
        except management.ProviderError as exc:
            self.reply(502, {'error': str(exc)})
        except Exception:
            self.reply(500, {'error': 'Interner Fehler. Konfiguration und Datenvolume prüfen.'})

    def get(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.reply(200, {'status': 'ok'})
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        if path in assets:
            name, kind = assets[path]
            return self.reply(200, (Path(__file__).parent / 'static' / name).read_text(encoding='utf-8'), kind)
        actor = self.actor()
        if not actor:
            return self.reply(401, {'error': 'Authentication required'})
        if path == '/api/me':
            return self.reply(200, actor)
        if path == '/api/settings':
            return self.reply(200, load_config(expand=False))
        if path == '/api/authorship':
            return self.reply(200, authorship.settings(control(), parse_qs(urlsplit(self.path).query).get('id', [''])[0]))
        if path == '/api/transport':
            return self.reply(200, transport.settings(control(), parse_qs(urlsplit(self.path).query).get('id', [''])[0]))
        if path == '/api/providers':
            return self.reply(200, {'providers': control().providers()})
        if path == '/api/users':
            if actor['role'] != 'admin':
                return self.reply(403, {'error': 'Administratorrechte erforderlich'})
            return self.reply(200, {'users': control().users()})
        if path == '/api/discover':
            params = parse_qs(urlsplit(self.path).query)
            return self.reply(200, control().discover(params.get('provider', [''])[0],
                params.get('owner', [''])[0], params.get('scope', ['self'])[0], int(params.get('page', ['1'])[0]), config=load_config()))
        if path in ('/api/status', '/api/jobs'):
            store = control()
            if path == '/api/jobs':
                return self.reply(200, {'jobs': store.jobs()})
            with database() as db:
                rows = db.execute('SELECT time, repository, status, details FROM runs ORDER BY id DESC LIMIT 100').fetchall()
            config = load_config()
            worker = WORKER.status() if WORKER else runtime.status(store)
            return self.reply(200, {'running': bool(worker['active']), 'actor': actor, 'worker': worker, 'snapshot_time': time.time(),
                'repositories': [{'name': r['name'], 'remotes': list(r['remotes']), 'enabled': r.get('enabled', True),
                                  'direction': r.get('direction', 'bidirectional'), 'source': r.get('source'),
                                  'remote_urls': r['remotes'], 'policy': r.get('policy', 'ff-only'),
                                  'branches': r.get('branches', []), 'tags': r.get('tags', True), 'selected_components': selected_components(r),
                                  'interval': r.get('interval', config.get('interval', 300))} for r in config['repositories']],
                'resolved_conflicts': conflict_store.resolved(store),
                'history': [dict(time=t, repository=r, status=status, details=json.loads(details))
                            for t, r, status, details in rows], 'jobs': store.jobs(), 'schedules': store.schedules()})
        self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        try:
            self.post()
        except (inspection.StaleState, runtime.Busy) as exc:
            self.reply(409, {'error': str(exc)})
        except GitFailure as exc:
            self.reply(502, dict(exc.details, error=exc.details['message']))
        except (ValueError, TypeError, KeyError, UnicodeError, AttributeError) as exc:
            # Validation errors contain no credential values.
            self.reply(400, {'error': str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Ungültige Anfrage'})
        except management.RateLimited as exc:
            self.reply(429, {'error': str(exc)})
        except management.ProviderError as exc:
            self.reply(502, {'error': str(exc)})
        except Exception:
            self.reply(500, {'error': 'Anfrage fehlgeschlagen. Datenvolume und Konfiguration prüfen.'})

    def post(self):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 <= size <= 1048576:
            return self.reply(413, {'error': 'Payload too large'})
        raw = self.rfile.read(size)
        if self.path.startswith('/hooks/'):
            return self.hook(raw)
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            raise ValueError('JSON-Objekt erwartet')
        store = control()
        if self.path == '/api/login':
            result = store.login(body.get('username'), body.get('password'), self.client_address[0])
            return self.reply(200 if result else 401, result or {'error': 'Anmeldung fehlgeschlagen'})
        actor = self.actor()
        if not actor:
            return self.reply(401, {'error': 'Authentication required'})
        if self.path == '/api/logout':
            store.logout(self.token())
            return self.reply(200, {'status': 'logged-out'})
        if self.path in ('/api/preview', '/api/refs', '/api/compare'):
            return self.reply(200, repository_action(self.path[5:], body, actor))
        if actor['role'] != 'admin':
            return self.reply(403, {'error': 'Lesebenutzer dürfen keine Änderungen vornehmen'})
        if self.path in ('/api/refs/change', '/api/merge', '/api/webhooks/register', '/api/conflicts/resolve'):
            return self.reply(200, repository_action(self.path[5:], body, actor))
        if self.path == '/api/settings':
            if not save_config(body):
                return self.reply(409, {'error': 'Andere Verwaltungsoperation läuft. Bitte erneut speichern.'})
            return self.reply(200, {'status': 'saved'})
        if self.path == '/api/sync':
            if body.get('shared_profile'):
                config = load_config()
                if body['shared_profile'] not in config.get('profiles', {}):
                    raise ValueError('Gemeinsames Profil nicht gefunden')
                identifiers = store.enqueue([r['name'] for r in config['repositories'] if r.get('profile') == body['shared_profile'] and r.get('enabled', True)], 'manual:' + actor['name'])
                return self.reply(202, {'jobs': identifiers})
            profile = body.get('profile')
            if profile is not None and profile not in [r['name'] for r in load_config()['repositories']]:
                raise ValueError('Profil nicht gefunden')
            identifiers = run_all(profile, 'manual:' + actor['name'])
            return self.reply(202, {'status': 'queued; duplicate requests are coalesced', 'jobs': identifiers})
        if self.path == '/api/providers':
            with LOCK, runtime.file_lock(DATA), runtime.file_lock(DATA, 'configuration'):
                if MAINTENANCE or WORKER and WORKER.active:
                    return self.reply(409, {'error': 'Aktive Syncs zuerst beenden lassen'})
                identifier = store.save_provider(body)
            return self.reply(200, {'id': identifier})
        if self.path == '/api/authorship':
            with LOCK, runtime.file_lock(DATA):
                if MAINTENANCE or WORKER and WORKER.active:
                    return self.reply(409, {'error': 'Aktive Syncs vor Aenderungen der Autoren-Zuordnung abschliessen lassen'})
                result = authorship.save(store, body)
            return self.reply(200, result)
        if self.path == '/api/transport':
            with LOCK, runtime.file_lock(DATA):
                return self.reply(200, transport.save(store, body))
        if self.path == '/api/providers/test':
            return self.reply(200, store.test_provider(body.get('id')))
        if self.path == '/api/providers/delete':
            with LOCK, runtime.file_lock(DATA), runtime.file_lock(DATA, 'configuration'):
                if MAINTENANCE or WORKER and WORKER.active:
                    return self.reply(409, {'error': 'Aktive Syncs zuerst beenden lassen'})
                store.delete_provider(body.get('id'), load_config())
            return self.reply(200, {'status': 'deleted'})
        if self.path == '/api/users':
            store.save_user(body)
            return self.reply(200, {'status': 'saved; sessions revoked'})
        if self.path == '/api/worker':
            modes = {'start': 'running', 'resume': 'running', 'pause': 'paused', 'stop': 'stopped'}
            action = body.get('action')
            if action not in modes:
                raise ValueError('Ungültige Worker-Aktion')
            if body.get('group') and body['group'] not in {'default', *load_config().get('worker_sets', {})}:
                raise ValueError('Worker-Gruppe nicht gefunden')
            with LOCK:
                mode = store.group_mode(body['group'], modes[action]) if body.get('group') else store.mode(modes[action])
            return self.reply(200, {'mode': mode})
        if self.path in ('/api/jobs/delete', '/api/jobs/reset'):
            return self.reply(200, store.manage_logs(self.path.rsplit('/', 1)[1], body, actor['name']))
        if self.path == '/api/jobs/cancel':
            with store.db() as db:
                db.execute("UPDATE jobs SET status='cancelled',finished=? WHERE id=? AND status='queued'",
                           (time.time(), body.get('id')))
            return self.reply(200, {'status': 'cancelled-if-queued'})
        self.reply(404, {'error': 'Not found'})

    def hook(self, body):
        parts = self.path.strip('/').split('/')
        if len(parts) != 3 or parts[0] != 'hooks' or parts[1] not in ('github', 'gitlab', 'forgejo'):
            return self.reply(404, {'error': 'Not found'})
        provider, name = parts[1:]
        repo = next((r for r in load_config()['repositories'] if r['name'] == name), None)
        secret = os.environ.get(repo.get('webhook_secret_env', ''), '') if repo else ''
        valid = False
        if secret:
            if provider == 'gitlab':
                valid = hmac.compare_digest(self.headers.get('X-Gitlab-Token', '').encode(), secret.encode())
            else:
                digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                header = 'X-Hub-Signature-256' if provider == 'github' else 'X-Forgejo-Signature'
                valid = hmac.compare_digest((self.headers.get(header, '') or (self.headers.get('X-Gitea-Signature', '') if provider == 'forgejo' else '')).encode(),
                                           (('sha256=' if provider == 'github' else '') + digest).encode())
        if not valid:
            return self.reply(403, {'error': 'Invalid webhook signature'})
        identifiers = run_all(name, 'webhook:' + provider)
        self.reply(202, {'status': 'queued', 'jobs': identifiers})


if __name__ == '__main__':
    if '--job' in sys.argv:
        runtime.run_child(sys.modules[__name__], int(sys.argv[sys.argv.index('--job') + 1]))
        raise SystemExit(0)
    role = os.environ.get('GITMESH_ROLE', 'combined')
    if role not in ('combined', 'web', 'worker'):
        raise SystemExit('GITMESH_ROLE must be web, worker or combined')
    if len(os.environ.get('GITMESH_ADMIN_TOKEN', '')) < 32:
        raise SystemExit('Set GITMESH_ADMIN_TOKEN to a random secret of at least 32 characters')
    for attempt in range(30):
        if CONFIG.exists():
            break
        if save_config({'interval': 300, 'repositories': []}):
            break
        time.sleep(0.1)
    load_config()
    with database():
        pass
    if role == 'worker':
        runtime.supervisor(sys.modules[__name__])
        raise SystemExit(0)
    instance = runtime.file_lock(DATA, 'web-instance')
    instance.__enter__()
    # Combined mode retains the single-container development entry point, with a separate supervisor process.
    worker_process = None
    if role == 'combined':
        worker_process = subprocess.Popen([sys.executable, str(Path(__file__).resolve())], env=dict(os.environ, GITMESH_ROLE='worker'),
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    server = ThreadingHTTPServer((os.environ.get('GITMESH_HOST', '0.0.0.0'), int(os.environ.get('GITMESH_PORT', '8080'))), Handler)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if worker_process:
            worker_process.terminate()
            worker_process.wait()
        instance.__exit__(None, None, None)
