"""GitMesh: conservative multi-remote Git synchronization, Python standard library only."""
from contextlib import contextmanager
import hashlib
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
from urllib.parse import urlsplit

DATA = Path(os.environ.get('GITMESH_DATA', '/data'))
CONFIG = Path(os.environ.get('GITMESH_CONFIG', str(DATA / 'gitmesh.json')))
LOCK = threading.Lock()
WAKE = threading.Event()


def git(path, *args, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')
    result = subprocess.run(['git', '-c', 'protocol.file.allow=never', '-c',
                             'protocol.ext.allow=never', '-C', str(path), *args],
                            capture_output=True, text=True, timeout=180, env=env)
    if check and result.returncode:
        # Git stderr can contain credentials or remote-controlled text.
        raise RuntimeError('Git operation failed: ' + args[0])
    return result


def validate_config(config):
    if not isinstance(config, dict) or set(config) - {'repositories', 'interval'}:
        raise ValueError('Invalid configuration fields')
    interval = config.get('interval', 300)
    if type(interval) is not int or not 30 <= interval <= 86400:
        raise ValueError('Intervall muss zwischen 30 und 86400 Sekunden liegen')
    repos = config.get('repositories')
    if not isinstance(repos, list) or len(repos) > 200:
        raise ValueError('Maximal 200 Repository-Netze erlaubt')
    names = set()
    for repo in repos:
        if not isinstance(repo, dict) or set(repo) - {'name', 'enabled', 'remotes', 'webhook_secret_env'}:
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
    return config


def load_config():
    return validate_config(json.loads(CONFIG.read_text(encoding='utf-8')))


def save_config(config):
    validate_config(config)
    if not LOCK.acquire(blocking=False):
        return False
    temporary = None
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CONFIG.parent,
                                         prefix='.gitmesh-', delete=False) as handle:
            temporary = handle.name
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG)
        temporary = None
        WAKE.set()
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
    for remote, url in repo['remotes'].items():
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
    actions, conflicts = [], []
    for ref in sorted(set().union(*(set(s) for s in snapshots.values()))):
        tips = {s[ref] for s in snapshots.values() if ref in s}
        if len(tips) == 1:
            winner = next(iter(tips))
        elif ref.startswith('tags/'):
            conflicts.append(ref)
            continue
        else:
            winners = [tip for tip in tips if all(
                other == tip or git(path, 'merge-base', '--is-ancestor', other, tip, check=False).returncode == 0
                for other in tips)]
            if len(winners) != 1:
                conflicts.append(ref)
                continue
            winner = winners[0]
        for remote, snapshot in snapshots.items():
            if snapshot.get(ref) != winner:
                # A regular push rejects concurrent divergence. Never force-push.
                git(path, 'push', remote, f'{winner}:refs/{ref}')
                actions.append(f'{remote}: {ref} -> {winner[:12]}')
    return ('conflict' if conflicts else 'success'), {'updated': actions, 'conflicts': conflicts}


def run_all(name=None):
    # Coalesce concurrent webhook requests; polling retries after active runs.
    if not LOCK.acquire(blocking=False):
        return
    try:
        for repo in load_config()['repositories']:
            if not repo.get('enabled', True) or (name and repo['name'] != name):
                continue
            try:
                status, details = synchronize(repo)
            except Exception as exc:
                status, details = 'error', {'message': type(exc).__name__ + ': sync failed; check connectivity and credentials'}
            with database() as db:
                db.execute('INSERT INTO runs(time, repository, status, details) VALUES(?,?,?,?)',
                           (time.time(), repo['name'], status, json.dumps(details)))
    finally:
        LOCK.release()


def poll():
    while True:
        try:
            run_all()
        except Exception:
            print('GitMesh configuration or scheduler error', flush=True)
        try:
            interval = load_config().get('interval', 300)
        except Exception:
            interval = 300
        WAKE.wait(interval)
        WAKE.clear()


class Handler(BaseHTTPRequestHandler):
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

    def authorized(self):
        expected = os.environ['GITMESH_ADMIN_TOKEN']
        return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + expected)

    def do_GET(self):
        if self.path == '/healthz':
            return self.reply(200, {'status': 'ok'})
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        if self.path in assets:
            name, kind = assets[self.path]
            return self.reply(200, (Path(__file__).parent / 'static' / name).read_text(encoding='utf-8'), kind)
        if not self.authorized():
            return self.reply(401, {'error': 'Authentication required'})
        if self.path == '/api/settings':
            return self.reply(200, load_config())
        if self.path == '/api/status':
            with database() as db:
                rows = db.execute('SELECT time, repository, status, details FROM runs ORDER BY id DESC LIMIT 100').fetchall()
            return self.reply(200, {'running': LOCK.locked(), 'repositories': [
                {'name': r['name'], 'remotes': list(r['remotes']), 'enabled': r.get('enabled', True)}
                for r in load_config()['repositories']], 'history': [
                    dict(time=t, repository=r, status=s, details=json.loads(d)) for t, r, s, d in rows]})
        self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 1048576:
                return self.reply(413, {'error': 'Payload too large'})
        except ValueError:
            return self.reply(400, {'error': 'Invalid length'})
        body = self.rfile.read(size)
        if self.path == '/api/settings':
            if not self.authorized():
                return self.reply(401, {'error': 'Authentication required'})
            try:
                config = json.loads(body)
                if not save_config(config):
                    return self.reply(409, {'error': 'Synchronisierung läuft. Bitte anschließend erneut speichern.'})
            except (ValueError, TypeError, KeyError, UnicodeError):
                return self.reply(400, {'error': 'Ungültige Einstellungen. Namen, URLs und Intervall prüfen.'})
            except OSError:
                return self.reply(500, {'error': 'Konfiguration konnte nicht gespeichert werden. Schreibrechte prüfen.'})
            return self.reply(200, {'status': 'saved'})
        if self.path == '/api/sync':
            if not self.authorized():
                return self.reply(401, {'error': 'Authentication required'})
            threading.Thread(target=run_all, daemon=True).start()
            return self.reply(202, {'status': 'requested; active runs are coalesced'})
        parts = self.path.strip('/').split('/')
        if len(parts) != 3 or parts[0] != 'hooks' or parts[1] not in ('github', 'gitlab', 'forgejo'):
            return self.reply(404, {'error': 'Not found'})
        provider, name = parts[1:]
        repo = next((r for r in load_config()['repositories'] if r['name'] == name), None)
        secret = os.environ.get(repo.get('webhook_secret_env', ''), '') if repo else ''
        valid = False
        if secret:
            if provider == 'gitlab':
                valid = hmac.compare_digest(self.headers.get('X-Gitlab-Token', ''), secret)
            else:
                digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                header = 'X-Hub-Signature-256' if provider == 'github' else 'X-Forgejo-Signature'
                valid = hmac.compare_digest(self.headers.get(header, ''), ('sha256=' if provider == 'github' else '') + digest)
        if not valid:
            return self.reply(403, {'error': 'Invalid webhook signature'})
        threading.Thread(target=run_all, args=(name,), daemon=True).start()
        self.reply(202, {'status': 'requested'})


if __name__ == '__main__':
    if len(os.environ.get('GITMESH_ADMIN_TOKEN', '')) < 32:
        raise SystemExit('Set GITMESH_ADMIN_TOKEN to a random secret of at least 32 characters')
    if not CONFIG.exists():
        save_config({'interval': 300, 'repositories': []})
    load_config()
    with database():
        pass
    threading.Thread(target=poll, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
