import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import entrypoint


def result(stdout='', code=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=code)


class KeyringStartupTests(unittest.TestCase):
    def test_private_keyring_unlock_and_probe_without_secret_logging(self):
        replies=[result('GNOME_KEYRING_CONTROL=/tmp/private\n'), result(), result(), result('ready\n'), result()]
        with patch.dict(os.environ, {'WEB_PASSWORD':'web-secret','KEYRING_PASSWORD':'stable-master'}, clear=True), patch.object(entrypoint.tempfile,'mkdtemp',return_value='/tmp/private-runtime'), patch.object(entrypoint.subprocess,'run',side_effect=replies) as run:
            entrypoint.prepare_keyring()
            self.assertEqual(run.call_args_list[0].kwargs['input'],'stable-master\n')
            self.assertEqual(os.environ['GNOME_KEYRING_CONTROL'],'/tmp/private')
            self.assertEqual(os.environ['MULTIGIT_KEYRING_READY'],'1')
            for call in run.call_args_list:
                self.assertNotIn('stable-master',str(call.args))

    def test_wrong_master_password_fails_before_web_start(self):
        with patch.dict(os.environ, {'WEB_PASSWORD':'secret'}, clear=True), patch.object(entrypoint.tempfile,'mkdtemp',return_value='/tmp/private-runtime'), patch.object(entrypoint.subprocess,'run',side_effect=[result(),result(),result(code=1)]):
            with self.assertRaisesRegex(RuntimeError,'gesperrt'):
                entrypoint.prepare_keyring()
            self.assertNotIn('MULTIGIT_KEYRING_READY',os.environ)

    def test_empty_or_multiline_password_rejected(self):
        for password in ('','invalid\npassword'):
            with patch.dict(os.environ, {'WEB_PASSWORD':password}, clear=True):
                with self.assertRaises(RuntimeError):
                    entrypoint.prepare_keyring()


if __name__ == '__main__':
    unittest.main()
