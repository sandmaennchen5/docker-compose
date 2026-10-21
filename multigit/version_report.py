"""Inspect published MultiGit images and write an architecture-specific YAML report."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import sqlite3
import ssl
import subprocess
import tomllib

PLATFORMS = ('linux/amd64', 'linux/arm64')
APP = Path(__file__).parent


def command(*arguments):
    result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=180)
    return (result.stdout or result.stderr).strip()


def probe():
    packages = {}
    for line in command('dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n').splitlines():
        name, version = line.split('\t', 1)
        packages[name] = version
    with (APP / 'build/Cargo.lock').open('rb') as handle:
        lock = tomllib.load(handle)
    with (APP / 'build/Cargo.toml').open('rb') as handle:
        manifest = tomllib.load(handle)
    vendors = json.loads((APP / 'assets/versions.json').read_text())
    vendors['xterm']['sha256'] = hashlib.sha256((APP / 'assets/xterm.js').read_bytes()).hexdigest()
    return dict(
        architecture=command('dpkg', '--print-architecture'),
        os=platform.freedesktop_os_release().get('PRETTY_NAME', ''),
        multigit=command('multigit', 'version'),
        upstream_revision=(APP / 'upstream-revision').read_text().strip(),
        python=platform.python_version(), git=command('git', '--version').removeprefix('git version '),
        openssh=command('ssh', '-V'), openssl=ssl.OPENSSL_VERSION, sqlite=sqlite3.sqlite_version,
        keyring=command('gnome-keyring-daemon', '--version').splitlines()[0],
        dbus=command('dbus-daemon', '--version').splitlines()[0],
        gnupg=command('gpg', '--version').splitlines()[0],
        nano=command('nano', '--version').splitlines()[0],
        builder=dict(rustc=(APP / 'build/rustc-version').read_text().strip(),
                     cargo=(APP / 'build/cargo-version').read_text().strip(),
                     package=manifest['package']['name'], version=manifest['package']['version']),
        rust_locked_packages=sorted([dict(name=item['name'], version=item['version'])
                              for item in lock['package']], key=lambda item: (item['name'], item['version'])),
        vendor_assets=vendors, packages=dict(sorted(packages.items())),
        python_packages=dict(sorted((item.metadata['Name'], item.version)
                                    for item in metadata.distributions() if item.metadata['Name'])))


def validate_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('A valid published image digest is required')
    return value


def platform_digests(index):
    found = {}
    for manifest in index.get('manifests', []):
        info = manifest.get('platform', {})
        target = info.get('os', '') + '/' + info.get('architecture', '')
        if target not in PLATFORMS:
            continue
        if target in found:
            raise ValueError('Ambiguous image manifest for ' + target)
        found[target] = validate_digest(manifest.get('digest'))
    if set(found) != set(PLATFORMS):
        raise ValueError('Published manifests for both image platforms are required')
    return found


def yaml_lines(value, indent=0):
    prefix = ' ' * indent
    for key, item in value.items():
        label = json.dumps(str(key), ensure_ascii=False)
        if isinstance(item, dict) and item:
            yield prefix + label + ':'
            yield from yaml_lines(item, indent + 2)
        else:
            yield prefix + label + ': ' + json.dumps(item, ensure_ascii=False)


def report(image, digest, revision, ref, measurements):
    validate_digest(digest)
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise ValueError('A source commit is required')
    if set(measurements) != set(PLATFORMS):
        raise ValueError('Versions for both image platforms are required')
    for target, measurement in measurements.items():
        if measurement['architecture'] != target.split('/')[1]:
            raise ValueError('Measured architecture does not match ' + target)
        validate_digest(measurement['image_digest'])
    return dict(build=dict(date=datetime.now(timezone.utc).strftime('%Y.%m.%d'),
                           measured_at=datetime.now(timezone.utc).isoformat(),
                           git_tag=ref if ref.startswith('multigit-v') else '',
                           git_revision=revision, image=image, image_tag='sha-' + revision,
                           image_digest=digest, platforms=list(PLATFORMS)),
                base=dict(image='debian:bookworm-slim', builder_image='rust:bookworm'),
                platforms=measurements)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--digest')
    parser.add_argument('--revision')
    parser.add_argument('--ref', default='')
    parser.add_argument('--output', default='multigit/versions.yaml')
    args = parser.parse_args(argv)
    if args.probe:
        print(json.dumps(probe()))
        return
    if not args.image or not args.digest or not args.revision:
        parser.error('--image, --digest and --revision are required')
    validate_digest(args.digest)
    index = json.loads(command('docker', 'buildx', 'imagetools', 'inspect', '--raw', args.image + '@' + args.digest))
    digests = platform_digests(index)
    measurements = {}
    for target in PLATFORMS:
        output = command('docker', 'run', '--rm', '--network', 'none', '--read-only',
                         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                         '--platform', target, '--entrypoint', 'python3', args.image + '@' + digests[target],
                         '-B', '/app/version_report.py', '--probe')
        measurements[target] = json.loads(output)
        measurements[target]['image_digest'] = digests[target]
        measurements[target]['image_built_at'] = command('docker', 'image', 'inspect', '--format', '{{.Created}}', args.image + '@' + digests[target])
    result = report(args.image, args.digest, args.revision, args.ref, measurements)
    Path(args.output).write_text('\n'.join(yaml_lines(result)) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
