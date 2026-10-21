"""Docker process supervisor and OS locks, shared by web and worker containers."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import management


class Busy(RuntimeError):
    pass


@contextmanager
def file_lock(data, name='operations', exclusive=True):
    directory = Path(data) / 'locks'
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / (name + '.lock'), 'a+b')
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError:
            raise Busy('Aktive Repository-Operation zuerst abschliessen lassen') from None
        yield
    finally:
        handle.close()


def retryable_push(exc):
    details = getattr(exc, 'details', {})
    text = details.get('git_error', '').lower()
    if any(x in text for x in ('authentication failed', 'permission denied', 'protected branch', 'pre-receive hook declined', 'stale info', 'non-fast-forward')):
        return False
    return details.get('operation') == 'push' and any(x in text for x in (
        'timed out', 'timeout', 'could not resolve', 'connection reset', 'connection refused',
        'remote end hung up', 'http 502', 'http 503', 'http 504', 'failed to connect', 'broken pipe'))


def limits(config, group):
    return dict(config.get('worker', {}), **config.get('worker_sets', {}).get(group, {}))


def publish(store, active, notice='', group_modes=None):
    with store.db() as db:
        db.execute("INSERT OR REPLACE INTO runtime_state VALUES(1,?,?)", (time.time(), json.dumps({
            'active': active, 'notice': notice, 'groups': group_modes or {}})))


def status(store):
    with store.db() as db:
        row = db.execute('SELECT * FROM runtime_state WHERE id=1').fetchone()
        jobs = db.execute("SELECT id,profile FROM jobs WHERE status='running'").fetchall()
    state = json.loads(row['details']) if row else {}
    heartbeat = row['heartbeat'] if row else None
    return dict(mode=store.mode(), active={str(r['id']): r['profile'] for r in jobs},
                heartbeat=heartbeat, online=bool(heartbeat and time.time() - heartbeat < 15),
                notice=state.get('notice', '') if heartbeat and time.time() - heartbeat < 15 else 'Worker-Container nicht erreichbar',
                groups=state.get('groups', {}), resources=management.resources(store.data))


def recover(store, skip=()):
    with store.db() as db:
        running = db.execute("SELECT id,profile FROM jobs WHERE status='running'").fetchall()
    for job in running:
        if job['id'] in skip:
            continue
        try:
            with file_lock(store.data, 'repo-' + job['profile']):
                with store.db() as db:
                    db.execute("UPDATE jobs SET status='error',finished=?,details=? WHERE id=? AND status='running'",
                               (time.time(), json.dumps({'message': 'Worker beendet; Job erneut ausloesen.'}), job['id']))
        except Busy:
            pass  # An orphaned child is still finishing this job; never claim it twice.


def run_child(mesh, identifier):
    store = mesh.control()
    with store.db() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=? AND status='running'", (identifier,)).fetchone()
    if not job:
        return
    try:
        with file_lock(store.data, exclusive=False), file_lock(store.data, 'repo-' + job['profile']):
            with store.db() as db:
                if not db.execute("SELECT 1 FROM jobs WHERE id=? AND status='running'", (identifier,)).fetchone():
                    return
            repo = next((r for r in mesh.load_config()['repositories'] if r['name'] == job['profile'] and r.get('enabled', True)), None)
            if not repo:
                store.finish(identifier, 'cancelled', {'message': 'Zuordnung entfernt oder deaktiviert'}, 0)
                return
            created = set()
            for attempt in range(1, 4):
                with store.db() as db:
                    db.execute('UPDATE jobs SET attempts=? WHERE id=?', (attempt, identifier))
                try:
                    outcome, details = mesh.execute_profile(repo)
                    created.update(details.get('created_repositories', []))
                    break
                except Exception as exc:
                    outcome = 'error'
                    details = getattr(exc, 'details', None) or {'message': 'Sync fehlgeschlagen. Konfiguration und Zugangsdaten pruefen.'}
                    created.update(details.get('created_repositories', []))
                    if attempt == 3 or not (getattr(exc, 'retryable', True) or retryable_push(exc)):
                        break
                    time.sleep(2 ** (attempt - 1))
                    # A retry executes the complete sync, including new fetches and fresh leases.
            if created:
                details['created_repositories'] = sorted(created)
            store.finish(identifier, outcome, details, attempt)
    except Busy:
        with store.db() as db:
            db.execute("UPDATE jobs SET status='queued',started=NULL WHERE id=? AND status='running'", (identifier,))


def supervisor(mesh):
    store = mesh.control()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    children = {}
    with file_lock(store.data, 'worker-instance'):
        try:
            while not stop.is_set():
                for identifier, (child, group) in list(children.items()):
                    if child.poll() is not None:
                        children.pop(identifier)
                notice, groups = '', {}
                try:
                    with file_lock(store.data, exclusive=False):
                        recover(store, children)
                        config = mesh.load_config()
                        configured = {'default': config.get('worker', {}), **config.get('worker_sets', {})}
                        if store.mode() == 'running':
                            scheduled = dict(config, repositories=[r for r in config['repositories'] if store.group_mode(r.get('worker_set', 'default')) == 'running'])
                            store.schedule(scheduled, time.time())
                        metrics = management.resources(store.data)
                        for group in configured:
                            settings = limits(config, group)
                            mode = store.group_mode(group)
                            groups[group] = {'mode': mode, 'parallelism': settings.get('parallelism', 2), 'active': sum(g == group for _, g in children.values())}
                            if store.mode() != 'running' or mode != 'running':
                                continue
                            if any(metrics[key] is not None and metrics[key] < settings.get(setting, default) for key, setting, default in [('disk_free_mb', 'min_free_mb', 128), ('memory_available_mb', 'min_memory_mb', 0)]) or settings.get('max_load', 0) and metrics['load'] is not None and metrics['load'] > settings['max_load']:
                                groups[group]['notice'] = 'Ressourcenlimit erreicht'
                                continue
                            names = [r['name'] for r in config['repositories'] if r.get('worker_set', 'default') == group and r.get('enabled', True)]
                            while True:
                                with store.db() as db:
                                    active_names = [row['profile'] for row in db.execute("SELECT profile FROM jobs WHERE status='running'")]
                                if sum(name in names for name in active_names) >= settings.get('parallelism', 2):
                                    break
                                job = store.claim(names)
                                if not job:
                                    break
                                try:
                                    child = subprocess.Popen([sys.executable, str(Path(mesh.__file__).resolve()), '--job', str(job['id'])],
                                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                             env=dict(os.environ, GITMESH_DATA=str(mesh.DATA), GITMESH_CONFIG=str(mesh.CONFIG)),
                                                             **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
                                    children[job['id']] = (child, group)
                                except OSError:
                                    store.finish(job['id'], 'error', {'message': 'Job-Prozess konnte nicht gestartet werden'}, 0)
                        # Disabled or deleted mappings must not leave immortal queued jobs.
                        with store.db() as db:
                            valid = {r['name'] for r in config['repositories'] if r.get('enabled', True)}
                            for row in db.execute("SELECT id,profile FROM jobs WHERE status='queued'").fetchall():
                                if row['profile'] not in valid:
                                    db.execute("UPDATE jobs SET status='cancelled',finished=? WHERE id=?", (time.time(), row['id']))
                except Busy:
                    notice = 'Manuelle Repository-Operation laeuft'
                except Exception:
                    notice = 'Worker-Konfiguration pruefen'
                publish(store, {str(k): g for k, (_, g) in children.items()}, notice, groups)
                stop.wait(1)
        finally:
            # Docker sends SIGTERM to the supervisor, which drains job processes.
            while children:
                children = {k: v for k, v in children.items() if v[0].poll() is None}
                publish(store, {str(k): g for k, (_, g) in children.items()}, 'Worker wird beendet')
                if children:
                    stop.wait(0.2) if not stop.is_set() else time.sleep(0.2)
            publish(store, {}, 'Worker beendet')
