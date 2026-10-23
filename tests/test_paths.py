import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_archive.paths import private_directory


class ProfilePathTests(unittest.TestCase):
    def test_source_keeps_existing_profile_location(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.frozen', False, create=True):
            self.assertEqual(private_directory(Path('/project')),Path('/project/.private'))

    def test_bundle_profile_is_outside_readonly_signed_app(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.frozen', True, create=True):
            profile=private_directory(Path('/Applications/Telegram Archive.app/Contents/Resources'))
            self.assertEqual(profile,Path.home()/'Library/Application Support/Telegram Archive')
            self.assertNotIn('.app',str(profile))

    def test_smoke_profile_override_does_not_use_real_account(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'TG_ARCHIVE_DATA_DIR':temporary}):
            self.assertEqual(private_directory(Path('/app')),Path(temporary).resolve())
