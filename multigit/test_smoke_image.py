import contextlib
import io
import json
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import smoke_image


class Response:
    def __init__(self, payload):
        self.stream = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self.stream

    def __exit__(self, *args):
        self.stream.close()


class SmokeImageTests(unittest.TestCase):
    def test_restart_uses_new_published_port(self):
        restarted = False
        sessions = 0
        requests = []

        def docker_run(arguments, **options):
            nonlocal restarted
            if arguments[:2] == ['docker', 'restart']:
                restarted = True

        def request(request, **options):
            nonlocal sessions
            url = urlsplit(request.full_url)
            expected = 49153 if restarted else 49152
            self.assertEqual(url.port, expected, 'Stale published port after restart')
            requests.append((url.port, url.path))
            if url.path == '/api/catalog':
                return Response(dict(terminal=True, keyring=True))
            if url.path == '/api/terminal/start':
                sessions += 1
                return Response(dict(id=str(sessions)))
            if url.path == '/api/terminal':
                output = 'multigit v2.0.0' if sessions == 1 else 'Commands:'
                return Response(dict(done=True, code=0, output=output))
            if url.path == '/api/overview':
                return Response(dict(clean=True, commits=[]))
            return Response(dict(ok=True))

        with patch.object(smoke_image.subprocess, 'run', side_effect=docker_run), patch.object(smoke_image.subprocess, 'check_output', side_effect=['127.0.0.1:49152\n', '127.0.0.1:49153\n']) as port, patch.object(smoke_image.urllib.request, 'urlopen', side_effect=request), contextlib.redirect_stdout(io.StringIO()):
            smoke_image.main('test-image')
        self.assertEqual(port.call_count, 2)
        self.assertEqual(requests[-1], (49153, '/api/catalog'))

    def test_missing_or_ambiguous_port_binding_fails_clearly(self):
        for binding in ('', '127.0.0.1:49152\n127.0.0.1:49153\n'):
            with self.subTest(binding=binding), patch.object(smoke_image.subprocess, 'check_output', return_value=binding):
                with self.assertRaisesRegex(RuntimeError, 'one published'):
                    smoke_image.published_address('smoke')


if __name__ == '__main__':
    unittest.main()
