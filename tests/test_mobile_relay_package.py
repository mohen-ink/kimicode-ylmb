import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mobile_relay as relay
import updater


class RelayPackageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / 'installed'
        self.target.mkdir()
        self.license = 'assets/vendor/frp-%s-LICENSE' % relay.RELAY_VERSION
        self.files = set(updater.PACKAGE_REQUIRED) | set(updater.MOBILE_RUNTIME_GROUP)
        self.files.update((self.license, 'assets/kimi-remote-widget.js',
                           'deploy/frp/frps.toml.example'))

    def package(self, exclude=()):
        path = self.base / 'plugin.zip'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(self.files - set(exclude)):
                archive.write(self.root / name, 'plugin/' + name)
        return path

    def test_complete_package_installs_final_sources(self):
        with mock.patch.object(updater, 'log'):
            copied = updater.apply_zip(str(self.package()), str(self.target))
        self.assertEqual(copied, len(self.files))
        for name in self.files:
            expected = hashlib.sha256((self.root / name).read_bytes()).digest()
            self.assertEqual(hashlib.sha256((self.target / name).read_bytes()).digest(),
                             expected, name)
        manifest = json.loads((self.target / 'kimi.plugin.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'], '3.3.8')

    def test_missing_relay_module_rejects_before_writing(self):
        sentinel = self.target / 'sentinel'
        sentinel.write_text('unchanged', encoding='utf-8')
        with self.assertRaises(updater.IncompletePackage):
            updater.apply_zip(str(self.package(('scripts/mobile_relay.py',))), str(self.target))
        self.assertEqual(list(self.target.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'unchanged')

    def test_missing_frp_license_rejects_before_writing(self):
        with self.assertRaises(updater.IncompletePackage):
            updater.apply_zip(str(self.package((self.license,))), str(self.target))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_new_runtime_is_not_deleted_by_sync(self):
        self.assertIn('scripts/mobile_relay.py', updater.MOBILE_RUNTIME_GROUP)
        self.assertFalse(updater.sync_delete_allowed('scripts/mobile_relay.py'))
        self.assertFalse(updater.sync_delete_allowed(self.license))


if __name__ == '__main__':
    unittest.main()
