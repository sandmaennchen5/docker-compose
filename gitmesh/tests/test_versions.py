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

    def test_each_architecture_is_pulled_even_if_index_digest_is_cached(self):
        reference = 'ghcr.io/example/gitmesh@sha256:' + 'a' * 64
        with patch.object(version_report, 'command', side_effect=['pulled amd64', '{"git":"amd64-version"}', 'pulled arm64', '{"git":"arm64-version"}']) as run:
            measurements = version_report.collect('ghcr.io/example/gitmesh', 'sha256:' + 'a' * 64)
        self.assertEqual(measurements['linux/arm64']['git'], 'arm64-version')
        self.assertEqual(run.call_count, 4)
        for offset, target in ((0, 'linux/amd64'), (2, 'linux/arm64')):
            self.assertEqual(run.call_args_list[offset].args, ('docker', 'pull', '--platform', target, reference))
            self.assertIn('--pull=never', run.call_args_list[offset + 1].args)
            self.assertIn(target, run.call_args_list[offset + 1].args)

    def test_docker_stderr_is_visible_on_failure(self):
        result = subprocess.CompletedProcess([], 125, '', 'image platform does not match linux/arm64')
        with patch.object(version_report.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'image platform does not match linux/arm64'):
                version_report.command('docker', 'run', 'example')
