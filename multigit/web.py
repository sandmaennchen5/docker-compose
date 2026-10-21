"""Authenticated MultiGit dashboard, settings API and interactive command sessions."""
import atexit
import base64
import hmac
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from catalog import CATALOG, build_command
from overview import overview
from settings import (load_settings, save_settings, save_remote, command_for,
                      config_document, save_document, remote_name, remote_url, git, effective_config, exclude_local_config)
from terminal import Sessions

ROOT = Path(os.environ.get('REPO_ROOT', '/repos')).resolve()
APP = Path(__file__).parent
LOCK = threading.Lock()
SESSIONS = Sessions()
atexit.register(SESSIONS.close)
COMMANDS = {
    'status': ['status'], 'init': ['init', '--no-interactive'], 'remotes': ['remote', 'list'],
    'preview': ['sync', '--dry-run'], 'sync': ['sync'],
    'push': ['push'], 'pull': ['pull'], 'doctor': ['doctor'],
}
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/assets/xterm.js': ('assets/xterm.js', 'text/javascript; charset=utf-8'),
    '/assets/xterm.css': ('assets/xterm.css', 'text/css; charset=utf-8'),
}


def repository(name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError('Ungueltiges Repository')
    path = (ROOT / name).resolve()
    if path == ROOT or ROOT not in path.parents or not (path / '.git').exists():
        raise ValueError('Repository muss unter /repos liegen und .git enthalten')
    return path


def new_repository(name):
    remote_name(name)
    target = ROOT / name
    if target.exists() or target.is_symlink() or ROOT not in target.resolve().parents:
        raise ValueError('Ziel existiert bereits oder liegt ausserhalb von /repos')
    return target


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, kind='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self):
        expected = 'Basic ' + base64.b64encode(
            (os.environ.get('WEB_USER', 'admin') + ':' + os.environ['WEB_PASSWORD']).encode()
        ).decode()
        if hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="MultiGit", charset="UTF-8"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'ok': True})
        if not self.authenticated():
            return
        route = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        try:
            if route in STATIC:
                filename, kind = STATIC[route]
                return self.reply(200, (APP / filename).read_bytes(), kind)
            if route == '/api/repos':
                names = sorted(p.name for p in ROOT.iterdir()
                               if p.is_dir() and (p / '.git').exists() and not p.is_symlink())
                return self.reply(200, names)
            if route == '/api/catalog':
                revision = APP / 'upstream-revision'
                return self.reply(200, dict(commands=list(CATALOG.values()),
                    terminal=os.name == 'posix', keyring=os.environ.get('MULTIGIT_KEYRING_READY') == '1', upstream_revision=revision.read_text().strip()
                    if revision.exists() else 'development'))
            if route == '/api/terminal':
                item = SESSIONS.get(query.get('id', [''])[0])
                cursor = int(query.get('cursor', ['0'])[0])
                return self.reply(200, item.snapshot(cursor))
            if route in ('/api/settings', '/api/config', '/api/overview', '/api/sessions'):
                repo = repository(query.get('repo', [''])[0])
                if route == '/api/sessions':
                    return self.reply(200, SESSIONS.list(repo))
                if not LOCK.acquire(blocking=False):
                    return self.reply(409, {'error': 'Ein Befehl laeuft bereits'})
                try:
                    if route == '/api/config':
                        data = config_document(repo, query.get('scope', ['repo'])[0])
                    elif route == '/api/overview':
                        data = overview(repo)
                        data['sessions'] = SESSIONS.list(repo)
                    else:
                        data = load_settings(repo)
                    return self.reply(200, data)
                finally:
                    LOCK.release()
            self.reply(404, {'error': 'Nicht gefunden'})
        except ValueError as error:
            self.reply(400, {'error': str(error)})
        except (OSError, subprocess.TimeoutExpired):
            self.reply(500, {'error': 'Datei oder Programm nicht verfuegbar. Volume-Rechte und Image pruefen.'})

    def terminal_control(self, payload):
        # Keystrokes and cancellation must not wait for Git/configuration I/O.
        try:
            item = SESSIONS.get(payload.get('id'))
            if self.path.endswith('/input'):
                item.write(payload.get('data'))
            elif self.path.endswith('/resize'):
                item.resize(payload.get('columns', 100), payload.get('rows', 30))
            else:
                item.stop()
            return self.reply(200, {'ok': True})
        except (ValueError, TypeError, OSError):
            return self.reply(400, {'error': 'Sitzung beendet oder ungueltige Terminaleingabe'})

    def do_POST(self):
        if not self.authenticated():
            return
        routes = ('/api/run', '/api/settings', '/api/remote', '/api/config',
                  '/api/terminal/start', '/api/terminal/input', '/api/terminal/resize',
                  '/api/terminal/stop', '/api/repos/create', '/api/repos/clone')
        if self.path not in routes or self.headers.get('X-MultiGit') != '1':
            return self.reply(403, {'error': 'Anfrage abgelehnt'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 98304:
                raise ValueError('Ungueltige Anfragegroesse')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('Ungueltige Anfrage')
        except (ValueError, TypeError):
            return self.reply(400, {'error': 'Ungueltige Anfrage'})
        if self.path in ('/api/terminal/input', '/api/terminal/resize', '/api/terminal/stop'):
            return self.terminal_control(payload)
        if not LOCK.acquire(blocking=False):
            return self.reply(409, {'error': 'Ein Befehl laeuft bereits'})
        try:
            if self.path in ('/api/repos/create', '/api/repos/clone'):
                if payload.get('confirmed') is not True:
                    raise ValueError('Anlegen bestaetigen')
                target = new_repository(payload.get('name'))
                if SESSIONS.busy():
                    return self.reply(409, {'error': 'Zuerst den laufenden Befehl beenden'})
                if self.path.endswith('/clone'):
                    url = remote_url(payload.get('url'))
                    item = SESSIONS.start(ROOT, ['clone', '--', url, target.name],
                                          'Repository klonen', target, True)
                    return self.reply(200, dict(id=item.id, repo=target.name))
                git(ROOT, 'init', '--', str(target))
                return self.reply(200, {'repo': target.name})
            repo = repository(payload['repo'])
            if self.path == '/api/terminal/start':
                operation = payload.get('operation')
                argv, mutates = build_command(operation, payload.get('arguments', ''))
                if mutates and payload.get('confirmed') is not True:
                    raise ValueError('Aendernden Befehl bestaetigen')
                if operation in ('init', 'setup') and (repo / '.git').is_dir():
                    exclude_local_config(repo)
                general = effective_config(repo).get('settings', {})
                options = payload.get('options', {})
                if not isinstance(options, dict):
                    raise ValueError('Ungueltige Programmoptionen')
                verbosity = options.get('verbosity')
                if verbosity is None:
                    verbosity = general.get('verbosity', 1)
                if type(verbosity) is not int or not 0 <= verbosity <= 4:
                    raise ValueError('Ungueltige Log-Stufe')
                for field in ('json', 'no_color'):
                    if type(options.get(field, False)) is not bool:
                        raise ValueError('Ungueltiger Programmschalter')
                if verbosity:
                    argv.insert(0, '-' + 'v' * verbosity)
                if general.get('colored_output') is False or options.get('no_color'):
                    argv.insert(0, '--no-color')
                if options.get('json'):
                    argv.insert(0, '--json')
                item = SESSIONS.start(repo, argv, CATALOG[operation]['label'], repo, mutates,
                                      background=operation == 'daemon-start',
                                      columns=payload.get('columns', 100), rows=payload.get('rows', 30))
                return self.reply(200, {'id': item.id})
            writing = self.path != '/api/run' or payload.get('action') in ('init', 'sync', 'push', 'pull')
            if writing and SESSIONS.busy():
                return self.reply(409, {'error': 'Zuerst den laufenden Befehl oder Daemon beenden'})
            if self.path == '/api/config':
                return self.reply(200, save_document(repo, payload))
            if self.path == '/api/settings':
                save_settings(repo, payload)
                return self.reply(200, load_settings(repo))
            if self.path == '/api/remote':
                save_remote(repo, payload)
                return self.reply(200, load_settings(repo))
            action = payload['action']
            command = command_for(repo, action, COMMANDS[action])
            result = subprocess.run(['multigit', *command], cwd=repo,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=120,
                                    encoding='utf-8', errors='replace')
            self.reply(200, {'code': result.returncode, 'output': result.stdout})
        except (ValueError, KeyError, TypeError) as error:
            message = str(error) if isinstance(error, ValueError) else 'Ungueltige Einstellungen oder Aktion'
            self.reply(400, {'error': message})
        except subprocess.TimeoutExpired:
            self.reply(504, {'error': 'Zeitlimit erreicht. Repository-Status pruefen.'})
        except OSError:
            self.reply(500, {'error': 'Dateizugriff oder Programmstart fehlgeschlagen. Volume-Rechte pruefen.'})
        finally:
            LOCK.release()


if __name__ == '__main__':
    if not os.environ.get('WEB_PASSWORD'):
        raise SystemExit('WEB_PASSWORD muss gesetzt sein')
    ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('0.0.0.0', 8080), Handler)
    try:
        server.serve_forever()
    finally:
        SESSIONS.close()
        server.server_close()
