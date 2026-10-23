"""Native integration checks; opt in with TG_ARCHIVE_GUI_TESTS=1 on a desktop."""
import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_archive.gui import App


def summary(phase='media', **overrides):
    data = dict(phase=phase, history_seen=428, history_total_hint=428, files_total=301,
                files_done=129, files_pending=170, files_errors=1, files_unavailable=1,
                total_bytes=4096000, done_bytes=2096000, active_id=12, active_bytes=1024,
                waiting=False, speed_bps=46000, eta_seconds=44, elapsed_seconds=95)
    data.update(overrides)
    return data


@unittest.skipUnless(os.environ.get('TG_ARCHIVE_GUI_TESTS') == '1', 'Native desktop check is opt-in')
class NativeInterfaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name)
        for mock in (patch('telegram_archive.gui.PRIVATE', self.private),
                     patch.object(App, '_load'), patch('telegram_archive.gui.find_tdata', return_value=[])):
            mock.start()
            self.addCleanup(mock.stop)
        self.app = App()
        self.app.update()

    def tearDown(self):
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def populate(self):
        self.app.connected = True
        self.app.dialogs = [
            dict(id=-100123, title='Архитектура и путешествия — фотографии городов и заметки о маршрутах', public=False, protected=True, account_id=123),
            dict(id=-100456, title='Книги и чтение', public=True, protected=False, account_id=123),
        ]
        self.app._filter()
        self.app.tree.selection_set('-100123')
        self.app._selection_changed()

    def assert_controls_fit(self):
        self.app.update_idletasks()
        # Catch controls silently clipped by pack when progress or long titles grow.
        def check(widget):
            if not widget.winfo_ismapped():
                return
            if widget.winfo_class() in ('TButton', 'TLabel', 'TCombobox', 'TEntry'):
                self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), str(widget))
                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget))
            for child in widget.winfo_children():
                check(child)
        check(self.app)

    def test_compact_window_fits_welcome_and_active_download(self):
        self.app.geometry('880x690')
        self.app.update()
        self.assertTrue(self.app.export_button.instate(['disabled']))
        self.assert_controls_fit()
        self.populate()
        self.app._summary(summary())
        self.app.progress_text.set('Скачиваем файлы канала')
        self.app.update()
        self.assert_controls_fit()
        self.assertEqual(self.app.metric_values[1].get(), '129/301')
        self.assertTrue(self.app.file_line.winfo_ismapped())

    def test_search_removes_stale_selection_and_busy_prevents_second_export(self):
        self.populate()
        self.assertFalse(self.app.export_button.instate(['disabled']))
        self.app.search.set('книги')
        self.assertEqual(len(self.app.visible_dialogs), 1)
        self.assertTrue(self.app.export_button.instate(['disabled']))
        self.app.tree.selection_set('-100456')
        self.app._selection_changed()
        self.assertFalse(self.app.export_button.instate(['disabled']))
        self.app._busy(True)
        self.assertTrue(self.app.export_button.instate(['disabled']))
        self.assertFalse(self.app.stop_button.instate(['disabled']))
        self.app._busy(False)
        self.assertFalse(self.app.export_button.instate(['disabled']))

    def test_settings_validation_is_inline_and_values_survive_reopening(self):
        self.app.login_method.set('QR-код')
        self.app._toggle_settings()
        self.app.fields['api_id'].set('invalid')
        self.app._save_settings()
        self.assertTrue(self.app.settings_visible)
        self.assertIn('API ID', self.app.settings_error.get())
        self.assertFalse((self.private / 'settings.json').exists())
        self.app._close_settings()
        self.app._toggle_settings()
        self.app.update()
        self.assertEqual(self.app.fields['api_id'].get(), 'invalid')
        self.assert_controls_fit()

    def test_completion_with_missing_files_remains_visible(self):
        self.app._summary(summary('finished'))
        self.assertEqual(self.app.phase_text.get(), 'Завершено с пропусками')
        self.assertEqual(self.app.phase_badge.cget('style'), 'ErrorBadge.TLabel')
        self.assertEqual(self.app.metric_values[2].get(), '1 / 1')
        self.assertFalse(self.app.file_line.winfo_ismapped())
        self.app._summary(summary('cancelled'))
        self.assertIn('можно продолжить', self.app.phase_text.get())


if __name__ == '__main__':
    unittest.main()
