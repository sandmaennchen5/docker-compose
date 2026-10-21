"""Version reports describe built image contents, never host dependency versions."""
import json
import subprocess
import unittest
from unittest.mock import patch
from test_sync import mesh
import version_report


class VersionReportTests(unittest.TestCase):
    def test_report_requires_both_platforms_and_real_digest(self):
        digest = 'sha256:' + 'a' * 64
        platforms = {target: {'python': '3.13.9', 'git': '2.47.3'} for target in version_report.PLATFORMS}
        result = version_report.report('ghcr.io/example/gitmesh', digest, 'b' * 40, 'gitmesh-v1.2.3', platforms)
        self.assertEqual(result['build']['image_digest'], digest)
        self.assertEqual(result['build']['git_tag'], 'gitmesh-v1.2.3')
        self.assertEqual(result['platforms'], platforms)
        with self.assertRaises(ValueError):
            version_report.report('image', digest, 'revision', '', {'linux/amd64': {}})
        with self.assertRaises(ValueError):
            version_report.report('image', 'latest', 'revision', '', platforms)

    def test_yaml_quotes_ambiguous_and_multiline_values(self):
        value = {'git': '2.47.3', 'special': 'value: "test"\nsecond line', 'platforms': ['linux/amd64', 'linux/arm64']}
        lines = list(version_report.yaml_lines(value))
        self.assertEqual(json.loads(lines[1].split(': ', 1)[1]), value['special'])
        self.assertEqual(json.loads(lines[2].split(': ', 1)[1]), value['platforms'])

    def test_platform_manifest_references_prevent_classic_docker_digest_collision(self):
        index = 'sha256:' + 'a' * 64
        children = {'linux/amd64': 'sha256:' + 'b' * 64, 'linux/arm64': 'sha256:' + 'c' * 64}
        manifests = [{'digest': digest, 'platform': dict(zip(('os', 'architecture'), target.split('/')))} for target, digest in children.items()]
        manifests.append({'digest': 'sha256:' + 'd' * 64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}})
        cache = {}
        def docker(*args):
            if args[:3] == ('docker', 'manifest', 'inspect'):
                return json.dumps({'manifests': manifests})
            target = args[args.index('--platform') + 1]
            reference = 'ghcr.io/example/gitmesh@' + children[target]
            self.assertIn(reference, args)
            self.assertNotIn('ghcr.io/example/gitmesh@' + index, args)
            if args[1] == 'pull':
                if reference in cache and cache[reference] != target:
                    raise RuntimeError('cannot overwrite digest')
                cache[reference] = target
                return 'pulled'
            self.assertEqual(cache[reference], target)
            self.assertIn('--pull=never', args)
            return json.dumps({'git': target + '-version'})
        with patch.object(version_report, 'command', side_effect=docker):
            measurements = version_report.collect('ghcr.io/example/gitmesh', index)
        self.assertEqual(measurements['linux/arm64']['git'], 'linux/arm64-version')
        self.assertEqual(measurements['linux/arm64']['image_digest'], children['linux/arm64'])
        self.assertEqual(len(cache), 2)

    def test_missing_platform_fails_before_importing_any_image(self):
        with patch.object(version_report, 'command', return_value=json.dumps({'manifests': []})) as run:
            with self.assertRaisesRegex(ValueError, 'linux/amd64'):
                version_report.collect('image', 'sha256:' + 'a' * 64)
        self.assertEqual(run.call_count, 1)

    def test_docker_stderr_is_visible_on_failure(self):
        result = subprocess.CompletedProcess([], 125, '', 'image platform does not match linux/arm64')
        with patch.object(version_report.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'image platform does not match linux/arm64'):
                version_report.command('docker', 'run', 'example')
