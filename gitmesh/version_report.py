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
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode:
        detail = (result.stderr or result.stdout or 'No diagnostic output').strip()
        raise RuntimeError('Command failed (exit ' + str(result.returncode) + '): ' + ' '.join(args) + '\n' + detail)
    return result.stdout.strip()


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


def valid_digest(digest):
    return isinstance(digest, str) and digest.startswith('sha256:') and len(digest) == 71 and all(c in '0123456789abcdef' for c in digest[7:])


def report(image, digest, revision, ref, measurements):
    if not valid_digest(digest):
        raise ValueError('A published image digest is required')
    if set(measurements) != set(PLATFORMS):
        raise ValueError('Versions for both image platforms are required')
    return {'build': {'date': datetime.now(timezone.utc).strftime('%Y.%m.%d'),
                      'git_tag': ref if ref.startswith('gitmesh-v') else '', 'git_revision': revision,
                      'image': image, 'image_tag': 'sha-' + revision, 'image_digest': digest, 'platforms': list(PLATFORMS)},
            'base': {'image': 'python:3.13-slim'}, 'platforms': measurements}


def platform_digests(image, digest):
    # Inspect the immutable registry index without importing it into Docker's local image store.
    index = json.loads(command('docker', 'manifest', 'inspect', image + '@' + digest))
    references = {}
    for target in PLATFORMS:
        matches = [entry.get('digest', '') for entry in index.get('manifests', [])
                   if entry.get('platform', {}).get('os', '') + '/' + entry.get('platform', {}).get('architecture', '') == target]
        if len(matches) != 1 or not valid_digest(matches[0]):
            raise ValueError('Exactly one valid platform manifest required for ' + target)
        references[target] = matches[0]
    return references


def collect(image, digest):
    if not valid_digest(digest):
        raise ValueError('A published image digest is required')
    measurements = {}
    for target, child_digest in platform_digests(image, digest).items():
        # Use each platform's manifest digest, never the shared index digest. The classic
        # Docker store cannot overwrite an index reference already associated with another architecture.
        reference = image + '@' + child_digest
        command('docker', 'pull', '--platform', target, reference)
        output = command('docker', 'run', '--rm', '--pull=never', '--network', 'none', '--platform', target,
                         '--entrypoint', 'python', reference, '/app/version_report.py', '--probe')
        measurements[target] = dict(json.loads(output), image_digest=child_digest)
    return measurements


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
    measurements = collect(args.image, args.digest)
    result = report(args.image, args.digest, args.revision, args.ref, measurements)
    Path(args.output).write_text('\n'.join(yaml_lines(result)) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
