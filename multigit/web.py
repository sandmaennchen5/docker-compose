"""Small authenticated UI for the upstream MultiGit CLI (no shell execution)."""
import base64
import hmac
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(os.environ.get('REPO_ROOT', '/repos')).resolve()
LOCK = threading.Lock()
COMMANDS = {
    'status': ['status'], 'init': ['init'], 'remotes': ['remote', 'list'],
    'preview': ['sync', '--dry-run'], 'sync': ['sync'],
    'push': ['push'], 'pull': ['pull'], 'doctor': ['doctor'],
}


def repository(name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError('Ungueltiges Repository')
    path = (ROOT / name).resolve()
    if path == ROOT or ROOT not in path.parents or not (path / '.git').exists():
        raise ValueError('Repository muss unter /repos liegen und .git enthalten')
    return path


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
        if self.path == '/':
            return self.reply(200, Path('/app/index.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path == '/api/repos':
            names = sorted(p.name for p in ROOT.iterdir()
                           if p.is_dir() and (p / '.git').exists() and not p.is_symlink())
            return self.reply(200, names)
        self.reply(404, {'error': 'Nicht gefunden'})

    def do_POST(self):
        if not self.authenticated():
            return
        # Custom header prevents cross-origin HTML forms from triggering commands.
        if self.path != '/api/run' or self.headers.get('X-MultiGit') != '1':
            return self.reply(403, {'error': 'Anfrage abgelehnt'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('Ungueltige Anfragegroesse')
            payload = json.loads(self.rfile.read(size))
            path = repository(payload['repo'])
            command = COMMANDS[payload['action']]
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {'error': 'Ungueltiges Repository oder Aktion'})
        if not LOCK.acquire(blocking=False):
            return self.reply(409, {'error': 'Ein Befehl laeuft bereits'})
        try:
            result = subprocess.run(['multigit', *command], cwd=path,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=120,
                                    encoding='utf-8', errors='replace')
            self.reply(200, {'code': result.returncode, 'output': result.stdout})
        except subprocess.TimeoutExpired:
            self.reply(504, {'error': 'Zeitlimit von 120 Sekunden erreicht. Repository-Status pruefen.'})
        except OSError:
            self.reply(500, {'error': 'MultiGit konnte nicht gestartet werden'})
        finally:
            LOCK.release()


if __name__ == '__main__':
    if not os.environ.get('WEB_PASSWORD'):
        raise SystemExit('WEB_PASSWORD muss gesetzt sein')
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
