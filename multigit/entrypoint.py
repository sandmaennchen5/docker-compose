"""Unlock the private container Secret Service, then run the web server."""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile


def prepare_keyring():
    password = os.environ.get('KEYRING_PASSWORD') or os.environ.get('WEB_PASSWORD')
    if not password or '\n' in password or '\r' in password:
        raise RuntimeError('KEYRING_PASSWORD bzw. WEB_PASSWORD muss gesetzt sein und darf keine Zeilenumbrueche enthalten')
    os.environ['XDG_RUNTIME_DIR'] = tempfile.mkdtemp(prefix='multigit-runtime-')
    result = subprocess.run(['gnome-keyring-daemon', '--unlock', '--components=secrets'],
                            input=password + '\n', text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Der private Zugangsdaten-Keyring konnte nicht gestartet werden')
    for line in result.stdout.splitlines():
        if line.startswith('GNOME_KEYRING_CONTROL='):
            os.environ['GNOME_KEYRING_CONTROL'] = line.split('=', 1)[1]
    started = subprocess.run(['gnome-keyring-daemon', '--start', '--components=secrets'],
                             capture_output=True, text=True, timeout=15)
    if started.returncode:
        raise RuntimeError('Der private Secret Service konnte nicht initialisiert werden')
    # Verify the same Secret Service that MultiGit uses. The probe contains no
    # user secret and is immediately removed again.
    probe = subprocess.run(['secret-tool', 'store', '--label=MultiGit startup check',
                            'application', 'multigit-webui', 'purpose', 'startup-check'],
                           input='ready', text=True, capture_output=True, timeout=15)
    if probe.returncode:
        raise RuntimeError('Zugangsdaten-Keyring gesperrt. Das bisherige KEYRING_PASSWORD beibehalten.')
    check = subprocess.run(['secret-tool', 'lookup', 'application', 'multigit-webui',
                            'purpose', 'startup-check'], capture_output=True, text=True, timeout=15)
    subprocess.run(['secret-tool', 'clear', 'application', 'multigit-webui',
                    'purpose', 'startup-check'], capture_output=True, timeout=15)
    if check.returncode or check.stdout.strip() != 'ready':
        raise RuntimeError('Der Zugangsdaten-Keyring antwortet nicht korrekt')
    os.environ['MULTIGIT_KEYRING_READY'] = '1'


if __name__ == '__main__':
    prepare_keyring()
    runpy.run_path(str(Path(__file__).with_name('web.py')), run_name='__main__')
