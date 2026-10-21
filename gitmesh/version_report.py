"""Probe installed image versions and create a YAML build report without dependencies."""
import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import platform
import sqlite3
import ssl
import subprocess

PLATFORMS = ('linux/amd64', 'linux/arm64')


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=180).stdout.strip()


def probe():
    packages = {}
    for name in ('git', 'openssh-client', 'ca-certificates'):
        packages[name] = command('dpkg-query', '-W', '-f=${Version}', name)
    ssh = subprocess.run(['ssh', '-V'], check=True, capture_output=True, text=True, timeout=10)
    os_release = platform.freedesktop_os_release()
    return {'python': platform.python_version(), 'git': command('git', '--version').removeprefix('git version '),
            'openssh': (ssh.stderr or ssh.stdout).strip(), 'openssl': ssl.OPENSSL_VERSION,
            'sqlite': sqlite3.sqlite_version, 'os': os_release.get('PRETTY_NAME', ''),
            'packages': packages, 'python_packages': {name: metadata.version(name) for name in ('cryptography', 'cffi', 'pycparser')}}


def yaml_lines(value, indent=0):
    prefix = ' ' * indent
    for key, item in value.items():
        # JSON strings are valid YAML scalars; quoting prevents ambiguous versions and shell-derived values.
        label = json.dumps(str(key), ensure_ascii=False)
        if isinstance(item, dict):
            yield prefix + label + ':'
            yield from yaml_lines(item, indent + 2)
        else:
            yield prefix + label + ': ' + json.dumps(item, ensure_ascii=False)


def report(image, digest, revision, ref, measurements):
    if not digest.startswith('sha256:') or len(digest) != 71 or any(c not in '0123456789abcdef' for c in digest[7:]):
        raise ValueError('A published image digest is required')
    if set(measurements) != set(PLATFORMS):
        raise ValueError('Versions for both image platforms are required')
    return {'build': {'date': datetime.now(timezone.utc).strftime('%Y.%m.%d'),
                      'git_tag': ref if ref.startswith('gitmesh-v') else '', 'git_revision': revision,
                      'image': image, 'image_tag': 'sha-' + revision, 'image_digest': digest, 'platforms': list(PLATFORMS)},
            'base': {'image': 'python:3.13-slim'}, 'platforms': measurements}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--digest')
    parser.add_argument('--revision')
    parser.add_argument('--ref', default='')
    parser.add_argument('--output', default='gitmesh/versions.yaml')
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe()))
        return
    if not args.image or not args.digest or not args.revision:
        parser.error('--image, --digest and --revision are required')
    measurements = {}
    for target in PLATFORMS:
        output = command('docker', 'run', '--rm', '--network', 'none', '--platform', target, '--entrypoint', 'python',
                         args.image + '@' + args.digest, '/app/version_report.py', '--probe')
        measurements[target] = json.loads(output)
    result = report(args.image, args.digest, args.revision, args.ref, measurements)
    Path(args.output).write_text('\n'.join(yaml_lines(result)) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
