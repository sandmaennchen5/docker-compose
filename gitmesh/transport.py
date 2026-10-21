"""Provider transport preferences and encrypted SSH credentials."""
from contextlib import contextmanager
import base64
import hashlib
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from urllib.parse import urlsplit


def secure_key(path):
    path.chmod(0o600)
    if os.name == 'nt':
        result = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', getpass.getuser() + ':(R,W)'], capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError('SSH-Schluesselrechte konnten nicht eingeschraenkt werden')


def settings(store, identifier, reveal=False):
    store.provider(identifier)
    with store.db() as db:
        row = db.execute('SELECT details FROM provider_transport WHERE id=?', (identifier,)).fetchone()
    value = json.loads(row['details']) if row else {}
    encrypted = value.pop('key', '')
    value['has_key'] = bool(encrypted)
    if reveal:
        value['key'] = store.cipher().decrypt(encrypted.encode()).decode() if encrypted else ''
    return {
        **dict(protocol='auto', ssh_user='git', ssh_host='', ssh_port=22, known_hosts=''), **value}


def save(store, body):
    identifier = body.get('id')
    provider = store.provider(identifier)
    protocol, host, user, port = body.get('protocol', 'auto'), body.get('ssh_host', ''), body.get('ssh_user', 'git'), body.get('ssh_port', 22)
    if protocol == 'http' and (not provider.get('allow_http') or urlsplit(provider['url']).scheme != 'http'):
        raise ValueError('HTTP-Transport benoetigt einen ausdruecklich freigegebenen HTTP-Provider')
    known, key = body.get('known_hosts', ''), body.get('key', '')
    if protocol not in ('auto', 'https', 'http', 'ssh') or not isinstance(host, str) or host and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host):
        raise ValueError('Transport oder SSH-Hostname ungueltig')
    if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', user) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('SSH-Benutzer oder Port ungueltig')
    if not isinstance(known, str) or len(known) > 131072 or not isinstance(key, str) or len(key) > 65536:
        raise ValueError('SSH-Eingabe zu gross')
    fingerprints = []
    for line in known.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) < 3 or not re.fullmatch(r'(ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+', parts[1]):
            raise ValueError('known_hosts: Host, Schluesseltyp und Schluessel erforderlich')
        try:
            data = base64.b64decode(parts[2], validate=True)
        except ValueError:
            raise ValueError('Ungueltiger SSH-Hostschluessel') from None
        if len(data) < 16:
            raise ValueError('Ungueltiger SSH-Hostschluessel')
        fingerprints.append('SHA256:' + base64.b64encode(hashlib.sha256(data).digest()).decode().rstrip('='))
    previous = settings(store, identifier, reveal=True)
    if previous['has_key'] and (previous.get('ssh_host', '') != host or previous.get('ssh_port', 22) != port) and not key and not body.get('clear_key'):
        raise ValueError('Bei SSH-Zielwechsel Schluessel neu eingeben oder entfernen')
    if key:
        with tempfile.TemporaryDirectory(prefix='gitmesh-key-') as directory:
            path = Path(directory) / 'key'
            path.write_text(key.replace('\r\n', '\n').rstrip() + '\n', encoding='utf-8', newline='\n')
            secure_key(path)
            try:
                result = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(path)], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError('SSH-Schluessel konnte nicht geprueft werden') from None
            if result.returncode:
                raise ValueError('Ungueltiger oder passwortgeschuetzter SSH-Schluessel; unverschluesselten Dienstschluessel verwenden')
        encrypted = store.cipher().encrypt(key.encode()).decode()
    else:
        encrypted = '' if body.get('clear_key') else (store.cipher().encrypt(previous['key'].encode()).decode() if previous['key'] else '')
    if encrypted and not known.strip():
        raise ValueError('Fuer gespeicherte SSH-Schluessel verifizierte known_hosts hinterlegen')
    value = dict(protocol=protocol, ssh_host=host, ssh_user=user, ssh_port=port, known_hosts=known, key=encrypted, fingerprints=fingerprints)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO provider_transport VALUES(?,?)', (identifier, json.dumps(value)))
    return settings(store, identifier)


def remote_url(store, repo, remote):
    original = repo['remotes'][remote]
    identifier = repo.get('remote_providers', {}).get(remote)
    if not identifier:
        return original
    value = settings(store, identifier)
    protocol = value['protocol']
    if protocol == 'auto':
        return original
    provider = store.provider(identifier)
    owner, name = store.repository_path(provider, original)
    if protocol in ('https', 'http'):
        if urlsplit(provider['url']).scheme != protocol or protocol == 'http' and not provider.get('allow_http'):
            raise ValueError('Git-Transport und Provider-URL muessen dasselbe HTTP(S)-Protokoll verwenden')
        return provider['url'].rstrip('/') + '/' + owner + '/' + name + '.git'
    host = value['ssh_host'] or urlsplit(provider['url']).hostname
    return 'ssh://' + value['ssh_user'] + '@' + host + (':' + str(value['ssh_port']) if value['ssh_port'] != 22 else '') + '/' + owner + '/' + name + '.git'


@contextmanager
def ssh_environment(store, repo, remote):
    identifier = repo.get('remote_providers', {}).get(remote)
    if not identifier or urlsplit(remote_url(store, repo, remote)).scheme != 'ssh':
        yield {}
        return
    value = settings(store, identifier, reveal=True)
    if not value['key']:
        # Existing mounted SSH configuration remains supported, always with strict host checking.
        yield {'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes', 'GIT_SSH_VARIANT': 'ssh'}
        return
    if not value['known_hosts'].strip():
        raise ValueError('Fuer gespeicherte SSH-Schluessel verifizierte known_hosts hinterlegen')
    with tempfile.TemporaryDirectory(prefix='gitmesh-ssh-') as directory:
        key, hosts = Path(directory) / 'key', Path(directory) / 'known_hosts'
        key.write_text(value['key'].replace('\r\n', '\n').rstrip() + '\n', encoding='utf-8', newline='\n')
        hosts.write_text(value['known_hosts'], encoding='utf-8')
        secure_key(key); hosts.chmod(0o600)
        command = 'ssh -F ' + shlex.quote(os.devnull) + ' -i ' + shlex.quote(key.as_posix()) + ' -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=' + shlex.quote(hosts.as_posix())
        yield {'GIT_SSH_COMMAND': command, 'GIT_SSH_VARIANT': 'ssh'}
