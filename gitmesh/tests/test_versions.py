"""Version reports describe built image contents, never host dependency versions."""
import json
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
