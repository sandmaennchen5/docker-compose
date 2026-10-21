"""Bounded, authenticated-by-caller Linux PTY sessions. No shell is launched."""
import codecs
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid

WORKER = Path(__file__).with_name('pty_worker.py')
MAX_OUTPUT = 1024 * 1024


def dimensions(columns, rows):
    if type(columns) is not int or type(rows) is not int:
        raise ValueError('Ungueltige Terminalgroesse')
    return max(40, min(columns, 240)), max(10, min(rows, 80))


class Session:
    def __init__(self, cwd, argv, label, repo_key, mutates, background=False, columns=100, rows=30):
        if os.name != 'posix':
            raise ValueError('Das interaktive Terminal steht im Linux-Docker-Image zur Verfuegung')
        import fcntl
        import pty
        import termios
        columns, rows = dimensions(columns, rows)
        self.id = uuid.uuid4().hex
        self.label = label
        self.repo_key = repo_key
        self.mutates = mutates
        self.background = background
        self.started = time.time()
        self.touched = time.monotonic()
        self.lock = threading.Lock()
        self.output = ''
        self.offset = 0
        self.done = False
        self.code = None
        master, slave = pty.openpty()
        self.master = master
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
            environment = dict(os.environ, TERM='xterm-256color', GIT_TERMINAL_PROMPT='1',
                               GIT_EDITOR='nano', EDITOR='nano', PAGER='cat', GIT_PAGER='cat')
            self.process = subprocess.Popen([sys.executable, str(WORKER), *argv], cwd=cwd,
                                            stdin=slave, stdout=slave, stderr=slave,
                                            start_new_session=True, env=environment,
                                            close_fds=True)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                chunk = os.read(self.master, 65536)
                if not chunk:
                    break
                self._append(decoder.decode(chunk))
        except OSError:
            pass  # Linux returns EIO when the PTY slave closes.
        finally:
            self._append(decoder.decode(b'', final=True))
            code = self.process.wait()
            with self.lock:
                self.code = code
                self.done = True
            try:
                os.close(self.master)
            except OSError:
                pass

    def _append(self, text):
        with self.lock:
            self.output += text
            if len(self.output) > MAX_OUTPUT:
                removed = len(self.output) - MAX_OUTPUT
                self.output = self.output[removed:]
                self.offset += removed

    def snapshot(self, cursor=0):
        if type(cursor) is not int or cursor < 0:
            raise ValueError('Ungueltige Ausgabeposition')
        self.touched = time.monotonic()
        with self.lock:
            end = self.offset + len(self.output)
            start = max(self.offset, min(cursor, end))
            return dict(id=self.id, label=self.label, output=self.output[start - self.offset:],
                        cursor=end, lost=cursor < self.offset, done=self.done, code=self.code,
                        started=self.started, background=self.background)

    def info(self):
        with self.lock:
            return dict(id=self.id, label=self.label, done=self.done, code=self.code,
                        started=self.started, background=self.background, mutates=self.mutates)

    def write(self, data):
        if not isinstance(data, str) or len(data.encode('utf-8')) > 16384:
            raise ValueError('Terminaleingabe ist zu gross')
        self.touched = time.monotonic()
        if self.process.poll() is not None:
            raise ValueError('Der Befehl ist bereits beendet')
        os.write(self.master, data.encode('utf-8'))

    def resize(self, columns, rows):
        import fcntl
        import termios
        columns, rows = dimensions(columns, rows)
        if self.process.poll() is None:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
            os.killpg(self.process.pid, signal.SIGWINCH)

    def stop(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            def reap():
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            threading.Thread(target=reap, daemon=True).start()


class Sessions:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.shutdown = threading.Event()
        threading.Thread(target=self._sweep, daemon=True).start()

    def _sweep(self):
        while not self.shutdown.wait(30):
            with self.lock:
                self._prune()

    def _prune(self):
        now = time.monotonic()
        for key, item in list(self.items.items()):
            idle = now - item.touched
            if idle > 1800 and not (item.background and item.process.poll() is None):
                item.stop()
                del self.items[key]
        while len(self.items) >= 24:
            finished = [item for item in self.items.values() if item.done]
            if not finished:
                break
            oldest = min(finished, key=lambda item: item.started)
            del self.items[oldest.id]

    def busy(self, repo_key=None):
        with self.lock:
            self._prune()
            return any((repo_key is None or item.repo_key == str(repo_key)) and item.mutates and
                       item.process.poll() is None for item in self.items.values())

    def start(self, cwd, argv, label, repo_key, mutates, **options):
        with self.lock:
            self._prune()
            live = [item for item in self.items.values() if item.process.poll() is None]
            if len(live) >= 4:
                raise ValueError('Maximal vier laufende Sitzungen. Zuerst einen Befehl beenden.')
            is_stop = argv[-2:] == ['daemon', 'stop']
            if mutates and not is_stop and any(item.mutates for item in live):
                raise ValueError('Es laeuft bereits ein schreibender Befehl. Zuerst beenden oder den Daemon stoppen.')
            item = Session(cwd, argv, label, str(repo_key), mutates, **options)
            self.items[item.id] = item
            return item

    def get(self, key):
        with self.lock:
            self._prune()
            if not isinstance(key, str) or key not in self.items:
                raise ValueError('Terminalsitzung nicht gefunden oder abgelaufen')
            return self.items[key]

    def list(self, repo_key):
        with self.lock:
            self._prune()
            return [item.info() for item in self.items.values() if item.repo_key == str(repo_key)]

    def close(self):
        self.shutdown.set()
        with self.lock:
            for item in self.items.values():
                item.stop()
