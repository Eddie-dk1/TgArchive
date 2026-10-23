"""Contract test between engine progress events and the native view renderer."""
import tempfile
import unittest
from pathlib import Path

from test_archive import FakeClient, channel, message
from telegram_archive.core import export_channel
from telegram_archive.gui import App


class TextValue:
    def __init__(self):
        self.value = ''
    def set(self, value):
        self.value = value


class ProgressViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_engine_events_render_in_native_view(self):
        view = type('View', (), {})()
        view.overall_text = TextValue()
        view.transfer_text = TextValue()
        view.file_text = TextValue()
        view.progress = {'value': 0}
        view.file_progress = {'value': 0}
        seen = set()
        def emit(kind, value):
            if kind == 'summary':
                App._summary(view, value)
                self.assertGreaterEqual(view.progress['value'], 0)
                self.assertLessEqual(view.progress['value'], 100)
                self.assertNotIn('None', view.transfer_text.value)
                seen.add(value['phase'])
        with tempfile.TemporaryDirectory() as temporary:
            await export_channel(FakeClient([message(1, payload=b'video')], {10:b'video'}),
                                 channel(), Path(temporary), emit)
        self.assertTrue({'history','media','finished'}.issubset(seen))
        self.assertIn('1/1', view.overall_text.value)
        self.assertEqual(view.progress['value'],100)
