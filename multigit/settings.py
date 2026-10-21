"""Repository settings shared by the HTTP API and command runner."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from urllib.parse import urlsplit


class SettingsError(ValueError):
    pass


def config_path(repo):
    folder = repo / '.multigit'
    target = folder / 'config.toml'
    if folder.is_symlink() or target.is_symlink():
        raise SettingsError('Verknuepfte Konfigurationsdateien werden nicht unterstuetzt')
    if repo not in target.resolve().parents:
        raise SettingsError('Konfiguration liegt ausserhalb des Repositorys')
    return target


def read_config(target):
    if not target.exists():
        return {}
    if target.stat().st_size > 262144:
        raise SettingsError('Konfigurationsdatei ist zu gross')
    try:
        with target.open('rb') as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError('Vorhandene TOML-Konfiguration ist ungueltig') from error
    for section in ('settings', 'sync', 'security', 'daemon', 'remotes'):
        if section in data and not isinstance(data[section], dict):
            raise SettingsError('Ungueltiger Konfigurationsabschnitt: ' + section)
    return data


def global_config():
    folder = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    return read_config(folder / 'multigit' / 'config.toml')


def effective_config(repo):
    data = copy.deepcopy(global_config())
    local = read_config(config_path(repo))
    for section, value in local.items():
        if isinstance(value, dict) and isinstance(data.get(section), dict):
            data[section].update(value)
        else:
            data[section] = value
    return data


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) in (int, float):
        return repr(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(key) + ' = ' + toml_value(item)
                               for key, item in value.items()) + '}'
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    raise SettingsError('Nicht unterstuetzter TOML-Wert')


def atomic_write(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.multigit-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def exclude_local_config(repo):
    result = git(repo, 'rev-parse', '--git-path', 'info/exclude')
    target = Path(result.stdout.strip())
    if not target.is_absolute():
        target = repo / target
    if target.is_symlink() or repo not in target.resolve().parents:
        raise SettingsError('Git-Ausschlussdatei muss innerhalb des Repositorys liegen')
    content = target.read_bytes() if target.exists() else b''
    if b'/.multigit/' not in content.splitlines():
        atomic_write(target, content + b'\n/.multigit/\n')


def write_config(repo, data):
    target = config_path(repo)
    content = ''.join(json.dumps(key) + ' = ' + toml_value(value) + '\n'
                      for key, value in data.items()).encode('utf-8')
    tomllib.loads(content.decode('utf-8'))
    exclude_local_config(repo)
    if target.exists():
        atomic_write(target.with_suffix('.toml.bak'), target.read_bytes())
    atomic_write(target, content)


def git(repo, *arguments, check=True):
    result = subprocess.run(['git', *arguments], cwd=repo, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=15)
    if check and result.returncode:
        # Git error output can contain credentials from existing remotes.
        raise SettingsError('Git-Konfiguration konnte nicht geaendert werden')
    return result


def remote_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise SettingsError('Remote-Name: nur Buchstaben, Zahlen, Punkt, Bindestrich und Unterstrich')
    return value


def remote_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(c.isspace() for c in value):
        raise SettingsError('Ungueltige Git-URL')
    parsed = urlsplit(value)
    if parsed.scheme in ('https', 'ssh') and parsed.hostname and parsed.path:
        if parsed.password or parsed.query or parsed.fragment or (parsed.scheme == 'https' and parsed.username):
            raise SettingsError('Keine Zugangsdaten oder URL-Parameter in Git-URLs speichern')
        return value
    if re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./~-]+', value):
        return value
    raise SettingsError('HTTPS- oder SSH-Git-URL verwenden')


def load_settings(repo):
    data = effective_config(repo)
    general = data.get('settings', {})
    sync = data.get('sync', {})
    configured = data.get('remotes', {})
    names = set(git(repo, 'remote').stdout.splitlines()) | set(configured)
    remotes = []
    for name in sorted(names):
        remote_name(name)
        result = git(repo, 'config', '--get', 'remote.' + name + '.url', check=False)
        url = result.stdout.strip()
        try:
            remote_url(url)
        except ValueError:
            url = ''  # Never send embedded credentials to the browser.
        item = configured.get(name, {})
        remotes.append({'name': name, 'url': url, 'username': item.get('username', ''),
                        'provider': item.get('provider') or 'github',
                        'api_url': item.get('api_url') or '',
                        'enabled': item.get('enabled', False),
                        'use_ssh': item.get('use_ssh', url.startswith('ssh:') or '@' in url)})
    return {'branch': general.get('default_branch', ''),
            'max_parallel': general.get('max_parallel', 4),
            'verbosity': general.get('verbosity', 1),
            'primary_source': sync.get('primary_source') or 'origin',
            'remotes': remotes}


def validate_branch(branch):
    if not isinstance(branch, str) or len(branch) > 255:
        raise SettingsError('Ungueltiger Branch')
    if branch and (branch.startswith(('-', '/')) or branch.endswith(('/', '.', '.lock'))
                   or '..' in branch or '@{' in branch or '//' in branch
                   or any(c.isspace() or ord(c) < 32 or c in '~^:?*[' or c == chr(92) for c in branch)):
        raise SettingsError('Ungueltiger Branch')
    return branch


def save_settings(repo, payload):
    branch = validate_branch(payload.get('branch', ''))
    if branch:
        git(repo, 'check-ref-format', '--branch', branch)
    parallel = payload.get('max_parallel')
    verbosity = payload.get('verbosity')
    if type(parallel) is not int or not 1 <= parallel <= 32:
        raise SettingsError('Parallele Vorgaenge: 1 bis 32')
    if type(verbosity) is not int or not 0 <= verbosity <= 3:
        raise SettingsError('Log-Stufe: 0 bis 3')
    primary = remote_name(payload.get('primary_source', 'origin'))
    data = read_config(config_path(repo))
    general = data.setdefault('settings', {})
    general.update(max_parallel=parallel, verbosity=verbosity)
    if branch:
        general['default_branch'] = branch
    else:
        general.pop('default_branch', None)
    data.setdefault('sync', {})['primary_source'] = primary
    write_config(repo, data)


def save_remote(repo, payload):
    name = remote_name(payload.get('name'))
    url = remote_url(payload.get('url'))
    provider = payload.get('provider')
    if provider not in ('github', 'gitlab', 'bitbucket', 'codeberg', 'gitea', 'forgejo'):
        raise SettingsError('Unbekannter Anbieter')
    username = payload.get('username', '')
    if not isinstance(username, str) or len(username) > 255 or any(ord(c) < 32 for c in username):
        raise SettingsError('Ungueltiger Benutzername')
    api_url = payload.get('api_url', '')
    if not isinstance(api_url, str):
        raise SettingsError('Ungueltige API-URL')
    if api_url:
        remote_url(api_url.rstrip('/') + '/')
        if urlsplit(api_url).scheme != 'https':
            raise SettingsError('API-URL muss HTTPS verwenden')
    for field in ('enabled', 'use_ssh'):
        if type(payload.get(field)) is not bool:
            raise SettingsError('Ungueltige Remote-Einstellung')
    data = read_config(config_path(repo))
    config = data.setdefault('remotes', {}).setdefault(name, {})
    config.update(username=username, provider=provider, enabled=payload['enabled'],
                  use_ssh=payload['use_ssh'])
    if api_url:
        config['api_url'] = api_url
    else:
        config.pop('api_url', None)
    old = git(repo, 'config', '--get', 'remote.' + name + '.url', check=False)
    if old.returncode == 0:
        git(repo, 'remote', 'set-url', name, url)
    else:
        git(repo, 'remote', 'add', name, url)
    try:
        write_config(repo, data)
    except Exception:
        if old.returncode == 0:
            git(repo, 'remote', 'set-url', name, old.stdout.strip())
        else:
            git(repo, 'remote', 'remove', name)
        raise


def command_for(repo, action, base):
    data = effective_config(repo)
    command = list(base)
    branch = data.get('settings', {}).get('default_branch', '')
    if action in ('sync', 'preview', 'push') and branch:
        command.extend(['--branch', validate_branch(branch)])
    if action == 'pull':
        command.extend(['--from', remote_name(data.get('sync', {}).get('primary_source') or 'origin')])
    return command
