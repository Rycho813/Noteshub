import contextlib
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_release import verify
from scripts.configure_ocr_credentials import write_credentials
from scripts.package_release import FILES, package


class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'release'
        for name in FILES:
            target=self.root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('synthetic release file')
        for name in ('noteshub/web.py','noteshub/ocr.py'):
            target=self.root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('# synthetic application')
        output=Path(self.temp.name)/'release.tar.gz'
        with contextlib.redirect_stdout(io.StringIO()):
            package(self.root,output)
        with tarfile.open(output) as archive:
            (self.root/'RELEASE.json').write_bytes(archive.extractfile('RELEASE.json').read())

    def test_exact_release_passes_and_changed_source_is_rejected(self):
        digest,count=verify(self.root)
        self.assertEqual(len(digest),64)
        self.assertGreater(count,10)
        (self.root/'noteshub/web.py').write_text('# changed')
        with self.assertRaises(ValueError): verify(self.root)

    def test_extra_application_files_and_symlinks_are_rejected(self):
        extra=self.root/'noteshub/unrecorded.py'
        extra.write_text('# extra')
        with self.assertRaises(ValueError): verify(self.root)
        extra.unlink()
        target=self.root/'noteshub/web.py'
        target.unlink()
        target.symlink_to(self.root/'noteshub/ocr.py')
        with self.assertRaises(ValueError): verify(self.root)

    def test_invalid_paths_cannot_make_checker_read_outside_release(self):
        path=self.root/'RELEASE.json'
        manifest=json.loads(path.read_text())
        manifest['sha256']['../outside']='0'*64
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): verify(self.root)


class CredentialWriterChecks(unittest.TestCase):
    def test_private_exclusive_creation_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'fake-test-credentials.json'
            capture=io.StringIO()
            with patch('scripts.configure_ocr_credentials.os.fchown') as chown, contextlib.redirect_stdout(capture):
                write_credentials(target,'fake-unit-id','fake-unit-key')
            chown.assert_called_once()
            self.assertEqual(chown.call_args.args[1:],(10001,10001))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o400)
            self.assertEqual(json.loads(target.read_text()),{'SecretId':'fake-unit-id','SecretKey':'fake-unit-key'})
            self.assertEqual(capture.getvalue(),'')
            with self.assertRaises(FileExistsError): write_credentials(target,'other-id','other-key')
            self.assertEqual(json.loads(target.read_text())['SecretKey'],'fake-unit-key')
