import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import version_report as versions

INDEX_DIGEST = 'sha256:' + 'a' * 64
AMD_DIGEST = 'sha256:' + 'b' * 64
ARM_DIGEST = 'sha256:' + 'c' * 64
REVISION = 'd' * 40


def index():
    return dict(manifests=[
        dict(platform=dict(os='linux', architecture='amd64'), digest=AMD_DIGEST),
        dict(platform=dict(os='linux', architecture='arm64', variant='v8'), digest=ARM_DIGEST),
        dict(platform=dict(os='unknown', architecture='unknown'), digest='sha256:'+'e'*64)])


class VersionReportTests(unittest.TestCase):
    def test_architecture_manifests_ignore_attestations_and_reject_missing_or_duplicate(self):
        self.assertEqual(versions.platform_digests(index()), {'linux/amd64': AMD_DIGEST, 'linux/arm64': ARM_DIGEST})
        missing=index(); missing['manifests'].pop(1)
        duplicate=index(); duplicate['manifests'].append(duplicate['manifests'][0])
        for value in (missing, duplicate):
            with self.assertRaises(ValueError):
                versions.platform_digests(value)

    def test_probe_measures_runtime_packages_and_preserves_build_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'build').mkdir(); (root/'assets').mkdir()
            (root/'build/Cargo.toml').write_text('[package]\nname="multigit"\nversion="2.0.0"\n')
            (root/'build/Cargo.lock').write_text('[[package]]\nname="crate"\nversion="1.0.0"\n[[package]]\nname="crate"\nversion="2.0.0"\n')
            (root/'build/rustc-version').write_text('rustc 1.90.0\nhost: x86_64-unknown-linux-gnu')
            (root/'build/cargo-version').write_text('cargo 1.90.0')
            (root/'upstream-revision').write_text(REVISION)
            (root/'assets/versions.json').write_text('{"xterm":{"version":"5.5.0"}}')
            (root/'assets/xterm.js').write_bytes(b'terminal-asset')
            def command(*args):
                if args[0]=='dpkg-query': return 'git\t1:2.0\nlibssl3:amd64\t3.0'
                if args[0]=='dpkg': return 'amd64'
                if args[0]=='multigit': return 'MultiGit v2.0.0'
                return args[0]+' version 1.0\nextra details'
            with patch.object(versions,'APP',root), patch.object(versions,'command',side_effect=command), patch.object(versions.platform,'freedesktop_os_release',return_value={'PRETTY_NAME':'Debian'}), patch.object(versions.metadata,'distributions',return_value=[SimpleNamespace(metadata={'Name':'example'},version='1.2')]):
                data=versions.probe()
        self.assertEqual(data['packages']['git'],'1:2.0')
        self.assertEqual(data['builder']['cargo'],'cargo 1.90.0')
        self.assertEqual(len(data['rust_locked_packages']),2)
        self.assertEqual(len(data['vendor_assets']['xterm']['sha256']),64)
        self.assertEqual(data['python_packages'],{'example':'1.2'})
        self.assertEqual(data['upstream_revision'],REVISION)

    def test_report_requires_real_platforms_and_preserves_digest_and_version_scalars(self):
        measurements={target:dict(architecture=target.split('/')[1],image_digest=digest,version='01.02: three # four') for target,digest in versions.platform_digests(index()).items()}
        data=versions.report('ghcr.io/example/multigit',INDEX_DIGEST,REVISION,'multigit-v1.0',measurements)
        self.assertEqual(data['build']['git_tag'],'multigit-v1.0')
        self.assertEqual(data['build']['image_digest'],INDEX_DIGEST)
        text='\n'.join(versions.yaml_lines(data))
        self.assertIn('"version": "01.02: three # four"',text)
        self.assertEqual(list(versions.yaml_lines({'python_packages':{}})),['"python_packages": {}'])
        measurements['linux/arm64']['architecture']='amd64'
        with self.assertRaises(ValueError):
            versions.report('image',INDEX_DIGEST,REVISION,'main',measurements)
        for invalid in ('latest','sha256:short','sha256:'+'z'*64):
            with self.assertRaises(ValueError):
                versions.validate_digest(invalid)

    def test_collection_runs_exact_platform_digests_without_keyring_entrypoint(self):
        calls=[]
        def command(*args):
            calls.append(args)
            if args[:3]==('docker','buildx','imagetools'): return json.dumps(index())
            if args[:3]==('docker','image','inspect'): return '2026-10-21T12:34:56Z'
            target=args[args.index('--platform')+1]
            return json.dumps(dict(architecture=target.split('/')[1],multigit='MultiGit v2.0.0'))
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'versions.yaml'
            with patch.object(versions,'command',side_effect=command):
                versions.main(['--image','ghcr.io/example/multigit','--digest',INDEX_DIGEST,'--revision',REVISION,'--output',str(output)])
            text=output.read_text()
        containers=[args for args in calls if args[:2]==('docker','run')]
        self.assertEqual(len(containers),2)
        self.assertIn('ghcr.io/example/multigit@'+AMD_DIGEST,containers[0])
        self.assertIn('ghcr.io/example/multigit@'+ARM_DIGEST,containers[1])
        self.assertIn('--read-only',containers[0])
        self.assertIn('python3',containers[0])
        self.assertIn('"image_built_at": "2026-10-21T12:34:56Z"',text)


if __name__ == '__main__':
    unittest.main()
