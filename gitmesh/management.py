"""Provider vault, local accounts, persistent jobs and worker supervision for GitMesh."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from cryptography.fernet import Fernet

KEY_LOCK = threading.Lock()
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Control:
    def __init__(self, data):
        self.data = Path(data)

    def acquire_instance(self):
        self.data.mkdir(parents=True, exist_ok=True)
        handle = open(self.data / 'instance.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError('Another GitMesh instance already uses this data directory') from None
        return handle

    @contextmanager
    def db(self):
        self.data.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.data / 'history.db', timeout=30)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS providers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
              url TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
              token TEXT NOT NULL DEFAULT '', health TEXT NOT NULL DEFAULT 'untested',
              checked REAL);
            CREATE TABLE IF NOT EXISTS users (
              name TEXT PRIMARY KEY, role TEXT NOT NULL, password TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (
              hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_failures (
              address TEXT PRIMARY KEY, count INTEGER NOT NULL, since REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY, profile TEXT NOT NULL, origin TEXT NOT NULL,
              status TEXT NOT NULL, requested REAL NOT NULL, started REAL,
              finished REAL, attempts INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL DEFAULT '{}');
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_job ON jobs(profile)
              WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS schedules (
              profile TEXT PRIMARY KEY, signature TEXT NOT NULL, next_run REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS worker_state (id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL);
            INSERT OR IGNORE INTO worker_state VALUES(1,'running');
        """)
        try:
            with db:
                yield db
        finally:
            db.close()

    def cipher(self):
        with KEY_LOCK:
            key = os.environ.get('GITMESH_VAULT_KEY', '').encode()
            if not key:
                keyfile = self.data / 'vault.key'
                if not keyfile.exists():
                    with self.db() as db:
                        if db.execute("SELECT 1 FROM providers WHERE token!='' LIMIT 1").fetchone():
                            raise ProviderError('Vault-Schlüssel fehlt. Backup wiederherstellen.')
                    descriptor = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, 'wb') as handle:
                        handle.write(Fernet.generate_key())
                        handle.flush()
                        os.fsync(handle.fileno())
                key = keyfile.read_bytes()
            return Fernet(key)

    def providers(self):
        with self.db() as db:
            return [dict(id=r['id'], name=r['name'], kind=r['kind'], url=r['url'],
                         username=r['username'], has_token=bool(r['token']),
                         health=r['health'], checked=r['checked'])
                    for r in db.execute('SELECT * FROM providers ORDER BY name')]

    def provider(self, identifier, reveal=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM providers WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Provider nicht gefunden')
        result = dict(row)
        result['token'] = self.cipher().decrypt(row['token'].encode()).decode() if reveal and row['token'] else ''
        return result

    def save_provider(self, body):
        identifier = body.get('id') or secrets.token_hex(8)
        name, kind, url = body.get('name', ''), body.get('kind'), body.get('url', '').rstrip('/')
        username, token = body.get('username', ''), body.get('token', '')
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('Ungültige Provider-ID')
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or kind not in ('github', 'gitlab', 'forgejo'):
            raise ValueError('Name und Provider-Typ prüfen')
        if not isinstance(url, str) or len(url) > 1024:
            raise ValueError('Ungültige Provider-URL')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or any(c.isspace() for c in url):
            raise ValueError('Provider-URL muss HTTPS ohne Zugangsdaten sein')
        parsed.port  # validate port
        url = parsed._replace(netloc=parsed.netloc.lower()).geturl().rstrip('/')
        if not isinstance(username, str) or len(username) > 100 or any(c.isspace() for c in username) or not isinstance(token, str) or len(token) > 8192 or any(c in token for c in '\r\n'):
            raise ValueError('Ungültige Zugangsdaten')
        with self.db() as db:
            previous = db.execute('SELECT * FROM providers WHERE id=?', (identifier,)).fetchone()
        encrypted = self.cipher().encrypt(token.encode()).decode() if token else (previous['token'] if previous else '')
        if previous and (previous['url'] != url or previous['kind'] != kind) and previous['token'] and not token and not body.get('clear_token'):
            raise ValueError('Bei Host- oder Typwechsel Token neu eingeben oder entfernen')
        if body.get('clear_token'):
            encrypted = ''
        with self.db() as db:
            db.execute("""INSERT INTO providers(id,name,kind,url,username,token) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,url=excluded.url,
                       username=excluded.username,token=excluded.token,health='untested',checked=NULL""",
                       (identifier, name, kind, url, username, encrypted))
        return identifier

    def delete_provider(self, identifier, config):
        if any(identifier in r.get('remote_providers', {}).values() for r in config['repositories']):
            raise ValueError('Provider wird noch von einem Profil verwendet')
        with self.db() as db:
            db.execute('DELETE FROM providers WHERE id=?', (identifier,))

    def api(self, provider, path, query=None):
        url = provider['url']
        if provider['kind'] == 'github':
            base = 'https://api.github.com' if url == 'https://github.com' else url + '/api/v3'
        else:
            base = url + ('/api/v4' if provider['kind'] == 'gitlab' else '/api/v1')
        headers = {'Accept': 'application/json', 'User-Agent': 'GitMesh'}
        if provider['token']:
            if provider['kind'] == 'gitlab':
                headers['PRIVATE-TOKEN'] = provider['token']
            else:
                headers['Authorization'] = ('Bearer ' if provider['kind'] == 'github' else 'token ') + provider['token']
        request = Request(base + path + ('?' + urlencode(query) if query else ''), headers=headers)
        try:
            with build_opener(NoRedirect()).open(request, timeout=20) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise ProviderError('Provider-Antwort zu groß')
                return json.loads(payload)
        except HTTPError as exc:
            exc.close()
            raise ProviderError('Provider-API antwortet mit HTTP ' + str(exc.code)) from None
        except (URLError, TimeoutError, ValueError, OSError):
            raise ProviderError('Provider-API nicht erreichbar oder ungültige Antwort') from None

    def test_provider(self, identifier):
        provider = self.provider(identifier, reveal=True)
        try:
            user = self.api(provider, '/user')
            login = user.get('login') or user.get('username')
            if not login:
                raise ProviderError('Provider lieferte kein Benutzerkonto')
        except ProviderError:
            with self.db() as db:
                db.execute("UPDATE providers SET health='error',checked=? WHERE id=?", (time.time(), identifier))
            raise
        with self.db() as db:
            db.execute("UPDATE providers SET health='ok',checked=?,username=CASE WHEN username='' THEN ? ELSE username END WHERE id=?",
                       (time.time(), login, identifier))
        return {'status': 'ok', 'account': login}

    def discover(self, identifier, owner='', scope='self', page=1):
        if scope not in ('self', 'org', 'user') or not isinstance(owner, str) or len(owner) > 200 or not 1 <= page <= 10000:
            raise ValueError('Discovery-Parameter prüfen')
        if scope != 'self' and not owner:
            raise ValueError('Organisation, Gruppe oder Benutzer fehlt')
        provider = self.provider(identifier, reveal=True)
        query = {'page': page, 'per_page': 50, 'limit': 50}
        escaped = quote(owner, safe='')
        if provider['kind'] == 'gitlab':
            if scope == 'org':
                path = '/groups/' + escaped + '/projects'
                query['include_subgroups'] = 'true'
            elif scope == 'user':
                path = '/users/' + escaped + '/projects'
            else:
                path = '/projects'
                query['membership'] = 'true'
        else:
            path = '/user/repos' if scope == 'self' else ('/orgs/' if scope == 'org' else '/users/') + escaped + '/repos'
        records = self.api(provider, path, query)
        if not isinstance(records, list):
            raise ProviderError('Ungültige Repository-Liste')
        repos = []
        for repo in records:
            name = repo.get('full_name') or repo.get('path_with_namespace')
            clone = repo.get('clone_url') or repo.get('http_url_to_repo')
            if not isinstance(name, str) or not isinstance(clone, str):
                continue
            parsed = urlsplit(clone)
            if parsed.scheme != 'https' or parsed.netloc != urlsplit(provider['url']).netloc or parsed.username:
                continue
            repos.append({'full_name': name, 'clone_url': clone,
                          'ssh_url': repo.get('ssh_url') or repo.get('ssh_url_to_repo', ''),
                          'private': bool(repo.get('private', repo.get('visibility') == 'private'))})
        return {'repositories': repos, 'page': page, 'next_page': page + 1 if len(records) == 50 else None}

    @staticmethod
    def password_hash(password, salt=None):
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
        return salt + ':' + digest

    def users(self):
        with self.db() as db:
            return [dict(name=r['name'], role=r['role'], enabled=bool(r['enabled']))
                    for r in db.execute('SELECT name,role,enabled FROM users ORDER BY name')]

    def save_user(self, body):
        name, role, password = body.get('name', ''), body.get('role'), body.get('password', '')
        enabled = body.get('enabled', True)
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or role not in ('admin', 'readonly') or type(enabled) is not bool:
            raise ValueError('Benutzername und Rolle prüfen')
        if not isinstance(password, str) or (password and not 12 <= len(password) <= 1024):
            raise ValueError('Passwort benötigt 12 bis 1024 Zeichen')
        encoded = self.password_hash(password) if password else None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM users WHERE name=?', (name,)).fetchone()
            if not old and not encoded:
                raise ValueError('Neuer Benutzer benötigt ein Passwort')
            if old and old['role'] == 'admin' and old['enabled'] and (role != 'admin' or not enabled):
                if db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0] <= 1:
                    raise ValueError('Letzter Administrator darf nicht deaktiviert werden')
            db.execute("""INSERT INTO users(name,role,password,enabled) VALUES(?,?,?,?)
                       ON CONFLICT(name) DO UPDATE SET role=excluded.role,password=excluded.password,enabled=excluded.enabled""",
                       (name, role, encoded or old['password'], int(enabled)))
            db.execute('DELETE FROM sessions WHERE username=?', (name,))

    def login(self, name, password, address):
        if not isinstance(name, str) or not isinstance(password, str) or len(name) > 80 or len(password) > 1024:
            return None
        now = time.time()
        with self.db() as db:
            fail = db.execute('SELECT * FROM login_failures WHERE address=?', (address,)).fetchone()
            if fail and fail['count'] >= 10 and now - fail['since'] < 900:
                raise RateLimited('Zu viele Anmeldeversuche. Nach 15 Minuten erneut versuchen.')
            user = db.execute('SELECT * FROM users WHERE name=?', (name,)).fetchone()
        salt = user['password'].split(':')[0] if user else '0' * 32
        candidate = self.password_hash(password, salt)
        valid = user and hmac.compare_digest(candidate, user['password']) and user['enabled']
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (now,))
            db.execute('DELETE FROM login_failures WHERE since<?', (now - 900,))
            if not valid:
                db.execute("""INSERT INTO login_failures VALUES(?,1,?) ON CONFLICT(address)
                           DO UPDATE SET count=count+1""", (address, now))
                return None
            db.execute('DELETE FROM login_failures WHERE address=?', (address,))
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES(?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), name, now + 8 * 3600))
        return {'token': token, 'name': name, 'role': user['role']}

    def actor(self, token):
        bootstrap = os.environ.get('GITMESH_ADMIN_TOKEN', '')
        if bootstrap and hmac.compare_digest(token.encode(), bootstrap.encode()):
            return {'name': 'bootstrap', 'role': 'admin'}
        if not token:
            return None
        with self.db() as db:
            row = db.execute("""SELECT u.name,u.role FROM sessions s JOIN users u ON s.username=u.name
                             WHERE s.hash=? AND s.expires>? AND u.enabled=1""",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def mode(self, value=None):
        with self.db() as db:
            if value is not None:
                if value not in ('running', 'paused', 'stopped'):
                    raise ValueError('Ungültiger Worker-Zustand')
                db.execute('UPDATE worker_state SET mode=? WHERE id=1', (value,))
            return db.execute('SELECT mode FROM worker_state WHERE id=1').fetchone()[0]

    def enqueue(self, names, origin='manual'):
        result = []
        with self.db() as db:
            for name in names:
                cursor = db.execute("INSERT OR IGNORE INTO jobs(profile,origin,status,requested) VALUES(?,?,'queued',?)",
                                    (name, origin, time.time()))
                if cursor.rowcount:
                    result.append(cursor.lastrowid)
        return result

    def jobs(self):
        with self.db() as db:
            return [dict(r, details=json.loads(r['details'])) for r in db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 100')]

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (time.time(), row['id']))
            return dict(row)

    def finish(self, identifier, status, details, attempts=1):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?,finished=?,details=?,attempts=? WHERE id=?',
                       (status, time.time(), json.dumps(details), attempts, identifier))

    def schedule(self, config, now):
        with self.db() as db:
            valid = {r['name'] for r in config['repositories'] if r.get('enabled', True) and r.get('interval', config.get('interval', 300))}
            for row in db.execute('SELECT profile FROM schedules').fetchall():
                if row['profile'] not in valid:
                    db.execute('DELETE FROM schedules WHERE profile=?', (row['profile'],))
            for repo in config['repositories']:
                interval = repo.get('interval', config.get('interval', 300))
                if not repo.get('enabled', True) or not interval:
                    continue
                signature = hashlib.sha256(json.dumps(repo, sort_keys=True).encode() + str(interval).encode()).hexdigest()
                row = db.execute('SELECT * FROM schedules WHERE profile=?', (repo['name'],)).fetchone()
                if not row or row['signature'] != signature:
                    offset = int(signature[:8], 16) % min(interval, 30)
                    db.execute('INSERT OR REPLACE INTO schedules VALUES(?,?,?)', (repo['name'], signature, now + offset))
                elif row['next_run'] <= now:
                    db.execute("INSERT OR IGNORE INTO jobs(profile,origin,status,requested) VALUES(?,'schedule','queued',?)", (repo['name'], now))
                    db.execute('UPDATE schedules SET next_run=? WHERE profile=?', (now + interval, repo['name']))

    def schedules(self):
        with self.db() as db:
            return {r['profile']: r['next_run'] for r in db.execute('SELECT profile,next_run FROM schedules')}


def resources(data):
    Path(data).mkdir(parents=True, exist_ok=True)
    result = {'disk_free_mb': shutil.disk_usage(data).free // (1024 * 1024),
              'load': os.getloadavg()[0] if hasattr(os, 'getloadavg') else None,
              'memory_available_mb': None}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                result['memory_available_mb'] = int(line.split()[1]) // 1024
    except OSError:
        pass
    return result


class Worker:
    """One supervisor with bounded parallel jobs. Running tasks finish on pause/stop."""
    def __init__(self, control, loader, execute, gate):
        self.control, self.loader, self.execute, self.gate = control, loader, execute, gate
        self.active = {}
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='gitmesh')
        self.heartbeat = None
        self.notice = ''
        with control.db() as db:
            db.execute("UPDATE jobs SET status='error',finished=?,details=? WHERE status='running'",
                       (time.time(), json.dumps({'message': 'Dienst während des Jobs beendet; erneut auslösen.'})))

    def tick(self):
        self.heartbeat = time.time()
        with self.gate:
            if self.control.mode() != 'running':
                return
            config = self.loader()
            limits = config.get('worker', {})
            metrics = resources(self.control.data)
            for actual, minimum in [('disk_free_mb', limits.get('min_free_mb', 128)),
                                    ('memory_available_mb', limits.get('min_memory_mb', 0))]:
                if metrics[actual] is not None and metrics[actual] < minimum:
                    self.notice = 'Ressourcenlimit: ' + actual
                    return
            if limits.get('max_load', 0) and metrics['load'] is not None and metrics['load'] > limits['max_load']:
                self.notice = 'Ressourcenlimit: CPU-Last'
                return
            self.notice = ''
            self.control.schedule(config, self.heartbeat)
            while len(self.active) < limits.get('parallelism', 2):
                job = self.control.claim()
                if not job:
                    break
                repo = next((r for r in config['repositories'] if r['name'] == job['profile'] and r.get('enabled', True)), None)
                if not repo:
                    self.control.finish(job['id'], 'cancelled', {'message': 'Profil entfernt oder deaktiviert'}, 0)
                    continue
                self.active[job['id']] = job['profile']
                self.pool.submit(self.work, job, repo)

    def work(self, job, repo):
        attempts = 0
        try:
            while attempts < 3:
                attempts += 1
                with self.control.db() as db:
                    db.execute('UPDATE jobs SET attempts=? WHERE id=?', (attempts, job['id']))
                try:
                    status, details = self.execute(repo)
                    break
                except Exception as exc:
                    status, details = 'error', {'message': type(exc).__name__ + ': Sync fehlgeschlagen. Verbindung, Rechte und Zugangsdaten prüfen.'}
                    if not getattr(exc, 'retryable', True):
                        break
                    if attempts < 3:
                        self.stop_event.wait(2 ** (attempts - 1))
            self.control.finish(job['id'], status, details, attempts)
        finally:
            with self.gate:
                self.active.pop(job['id'], None)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                self.notice = 'Worker-Fehler. Konfiguration und Datenvolume prüfen.'
            self.stop_event.wait(1)

    def status(self):
        with self.gate:
            active = dict(self.active)
        return {'mode': self.control.mode(), 'active': active, 'heartbeat': self.heartbeat,
                'notice': self.notice, 'resources': resources(self.control.data)}

    def close(self):
        self.stop_event.set()
        self.pool.shutdown(wait=True)
