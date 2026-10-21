import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import web


class RepositoryBoundaryTests(unittest.TestCase):
    def test_only_repositories_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'project' / '.git').mkdir(parents=True)
            with patch.object(web, 'ROOT', root):
                self.assertEqual(web.repository('project'), root / 'project')
                for invalid in ['../outside', '.', 'missing', str(root)]:
                    with self.assertRaises(ValueError):
                        web.repository(invalid)


if __name__ == '__main__':
    unittest.main()
