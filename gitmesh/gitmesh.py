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

DATA = Path(os.environ.get('GITMESH_DATA', '/data'))
CONFIG = Path(os.environ.get('GITMESH_CONFIG', str(DATA / 'gitmesh.json')))
LOCK = threading.Lock()
WORKER = None
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


def git(path, *args, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, LC_ALL='C')
    credentials = getattr(GIT_CONTEXT, 'credentials', {})
    remote = next((arg for arg in args[1:] if arg in credentials), None) if args[0] in ('fetch', 'push') else None
    if remote:
        username, token = credentials[remote]
        env.update(GIT_ASKPASS=str(Path(__file__).with_name('askpass.py')), GITMESH_GIT_USERNAME=username,
                   GITMESH_GIT_TOKEN=token)
    remote_name = next((arg for arg in args[1:] if not arg.startswith('-')), None) if args[0] in ('fetch', 'push') else None
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


def validate_config(config):
    if not isinstance(config, dict) or set(config) - {'repositories', 'interval', 'worker'}:
        raise ValueError('Invalid configuration fields')
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
        if not isinstance(repo, dict) or set(repo) - {'name', 'enabled', 'remotes', 'webhook_secret_env', 'direction', 'source', 'policy', 'branches', 'tags', 'interval', 'remote_providers', 'force_confirmed', 'create_missing', 'create_private'}:
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
        if type(repo.get('tags', True)) is not bool:
            raise ValueError('Ungültige Tag-Einstellung')
        if 'interval' in repo and (type(repo['interval']) is not int or repo['interval'] != 0 and not 30 <= repo['interval'] <= 86400):
            raise ValueError('Profilintervall: 0 (manuell) oder 30 bis 86400 Sekunden')
        providers = repo.get('remote_providers', {})
        if not isinstance(providers, dict) or set(providers) - set(remotes) or any(not isinstance(x, str) for x in providers.values()):
            raise ValueError('Ungültige Provider-Zuordnung')
        for option in ('create_missing', 'create_private'):
            if type(repo.get(option, option == 'create_private')) is not bool:
                raise ValueError('Invalid automatic repository creation setting')
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
            if parsed.scheme not in ('https', 'ssh') or not parsed.hostname or parsed.password or (parsed.scheme == 'https' and parsed.username):
                raise ValueError('HTTPS oder SSH ohne eingebettete Zugangsdaten verwenden')
            parsed.port
            if parsed.query or parsed.fragment:
                raise ValueError('Remote-URLs ohne Query oder Fragment verwenden')
            canonical = (parsed.hostname.lower(), parsed.path.rstrip('/').removesuffix('.git'))
            if canonical in claimed or canonical in profile_urls:
                raise ValueError('Repository ist bereits einem anderen Remote oder Profil zugeordnet')
            profile_urls.add(canonical)
        claimed.update(profile_urls)
    return config


def load_config():
    return validate_config(json.loads(CONFIG.read_text(encoding='utf-8')))


def save_config(config):
    validate_config(config)
    if not LOCK.acquire(blocking=False):
        return False
    temporary = None
    try:
        if WORKER is not None and WORKER.active:
            return False
        for repo in config['repositories']:
            for remote, identifier in repo.get('remote_providers', {}).items():
                provider = control().provider(identifier)
                url = urlsplit(repo['remotes'][remote])
                if repo.get('create_missing') and remote != repo['source']:
                    management.Control.repository_path(provider, repo['remotes'][remote])
                if url.scheme == 'https' and url.netloc != urlsplit(provider['url']).netloc:
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


def synchronize(repo):
    path = DATA / repo['name']
    path.mkdir(parents=True, exist_ok=True)
    if not (path / 'HEAD').exists():
        git(path, 'init', '--bare')
    snapshots = {}
    created = []
    remotes = list(repo['remotes'].items())
    if repo.get('create_missing'):
        if repo.get('source') not in repo['remotes']:
            raise ValueError('Automatic creation requires a source remote')
        # Fetch the source successfully before making any provider-side changes.
        remotes.sort(key=lambda item: item[0] != repo['source'])
    for remote, url in remotes:
        if repo.get('create_missing') and remote != repo['source']:
            identifier = repo.get('remote_providers', {}).get(remote)
            if not identifier:
                raise ValueError('Automatic creation requires a target provider')
            if control().ensure_repository(identifier, url, repo.get('create_private', True)):
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
    actions, conflicts, plan = [], [], []
    direction, policy = repo.get('direction', 'bidirectional'), repo.get('policy', 'ff-only')
    source = repo.get('source')
    references = set(snapshots[source]) if direction == 'one-way' else set().union(*(set(s) for s in snapshots.values()))
    for ref in sorted(references):
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
    if conflicts and policy == 'reject':
        return 'conflict', {'updated': [], 'conflicts': conflicts, 'created_repositories': created, 'message': 'Gesamtes Profil wegen Konflikt übersprungen'}
    for remote, ref, winner, old in plan:
        args = ['push']
        if policy == 'force-with-lease':
            args.append(f'--force-with-lease=refs/{ref}:{old or ""}')
        git(path, *args, remote, f'{winner}:refs/{ref}')
        actions.append(f'{remote}: {ref} -> {winner[:12]}')
    return ('conflict' if conflicts else 'success'), {'updated': actions, 'conflicts': conflicts, 'created_repositories': created}


def prepare_credentials(repo):
    credentials = {}
    for remote, identifier in repo.get('remote_providers', {}).items():
        provider = control().provider(identifier, reveal=True)
        parsed = urlsplit(repo['remotes'][remote])
        if parsed.scheme == 'https':
            if parsed.netloc != urlsplit(provider['url']).netloc:
                raise ValueError('Remote-URL und Provider stimmen nicht überein')
            if provider['token']:
                username = provider['username'] or ('oauth2' if provider['kind'] == 'gitlab' else 'x-access-token')
                credentials[remote] = (username, provider['token'])
    return credentials


def execute_profile(repo):
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



def run_all(name=None, origin='manual'):
    names = [r['name'] for r in load_config()['repositories'] if r.get('enabled', True) and (not name or r['name'] == name)]
    return control().enqueue(names, origin)


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
            return self.reply(200, load_config())
        if path == '/api/providers':
            return self.reply(200, {'providers': control().providers()})
        if path == '/api/users':
            if actor['role'] != 'admin':
                return self.reply(403, {'error': 'Administratorrechte erforderlich'})
            return self.reply(200, {'users': control().users()})
        if path == '/api/discover':
            params = parse_qs(urlsplit(self.path).query)
            return self.reply(200, control().discover(params.get('provider', [''])[0],
                params.get('owner', [''])[0], params.get('scope', ['self'])[0], int(params.get('page', ['1'])[0])))
        if path in ('/api/status', '/api/jobs'):
            store = control()
            if path == '/api/jobs':
                return self.reply(200, {'jobs': store.jobs()})
            with database() as db:
                rows = db.execute('SELECT time, repository, status, details FROM runs ORDER BY id DESC LIMIT 100').fetchall()
            config = load_config()
            worker = WORKER.status() if WORKER else {'mode': store.mode(), 'active': {}, 'heartbeat': None,
                                                     'notice': '', 'resources': management.resources(DATA)}
            return self.reply(200, {'running': bool(worker['active']), 'actor': actor, 'worker': worker,
                'repositories': [{'name': r['name'], 'remotes': list(r['remotes']), 'enabled': r.get('enabled', True),
                                  'direction': r.get('direction', 'bidirectional'), 'source': r.get('source'),
                                  'remote_urls': r['remotes'], 'policy': r.get('policy', 'ff-only'),
                                  'branches': r.get('branches', []), 'tags': r.get('tags', True),
                                  'interval': r.get('interval', config.get('interval', 300))} for r in config['repositories']],
                'history': [dict(time=t, repository=r, status=status, details=json.loads(details))
                            for t, r, status, details in rows], 'jobs': store.jobs(), 'schedules': store.schedules()})
        self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        try:
            self.post()
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
        if actor['role'] != 'admin':
            return self.reply(403, {'error': 'Lesebenutzer dürfen keine Änderungen vornehmen'})
        if self.path == '/api/settings':
            if not save_config(body):
                return self.reply(409, {'error': 'Synchronisierung läuft. Bitte anschließend erneut speichern.'})
            return self.reply(200, {'status': 'saved'})
        if self.path == '/api/sync':
            profile = body.get('profile')
            if profile is not None and profile not in [r['name'] for r in load_config()['repositories']]:
                raise ValueError('Profil nicht gefunden')
            identifiers = run_all(profile, 'manual:' + actor['name'])
            return self.reply(202, {'status': 'queued; duplicate requests are coalesced', 'jobs': identifiers})
        if self.path == '/api/providers':
            with LOCK:
                if WORKER and WORKER.active:
                    return self.reply(409, {'error': 'Aktive Syncs zuerst beenden lassen'})
                identifier = store.save_provider(body)
            return self.reply(200, {'id': identifier})
        if self.path == '/api/providers/test':
            return self.reply(200, store.test_provider(body.get('id')))
        if self.path == '/api/providers/delete':
            with LOCK:
                if WORKER and WORKER.active:
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
            with LOCK:
                mode = store.mode(modes[action])
            return self.reply(200, {'mode': mode})
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
                valid = hmac.compare_digest(self.headers.get(header, '').encode(),
                                           (('sha256=' if provider == 'github' else '') + digest).encode())
        if not valid:
            return self.reply(403, {'error': 'Invalid webhook signature'})
        identifiers = run_all(name, 'webhook:' + provider)
        self.reply(202, {'status': 'queued', 'jobs': identifiers})


if __name__ == '__main__':
    if len(os.environ.get('GITMESH_ADMIN_TOKEN', '')) < 32:
        raise SystemExit('Set GITMESH_ADMIN_TOKEN to a random secret of at least 32 characters')
    instance = control().acquire_instance()
    if not CONFIG.exists():
        save_config({'interval': 300, 'repositories': []})
    load_config()
    with database():
        pass
    WORKER = management.Worker(control(), load_config, execute_profile, LOCK)
    thread = threading.Thread(target=WORKER.loop, daemon=True)
    server = ThreadingHTTPServer((os.environ.get('GITMESH_HOST', '0.0.0.0'), int(os.environ.get('GITMESH_PORT', '8080'))), Handler)
    thread.start()
    def shutdown(*_):
        WORKER.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        thread.join(timeout=5)
        WORKER.close()
        instance.close()
