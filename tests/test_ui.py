"""Native integration checks; opt in with TG_ARCHIVE_GUI_TESTS=1 on a desktop."""
import os
import asyncio
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_miniapp_switch_catalogue_selection_and_compact_progress(self):
        self.populate()
        self.app.channel_dialogs = list(self.app.dialogs)
        self.app.current_account_id = 123
        self.app.source_mode.set('Мини-приложения')
        self.app._source_changed()
        self.assertEqual(self.app.tree.selection(),('edify',))
        self.assertEqual(self.app.export_button.cget('text'),'Обновить и докачать')
        self.app.mini_nodes = [dict(id='course:c',parent='',kind='course',title='Course',state='available'),
            dict(id='module:m',parent='course:c',kind='module',title='Module',state='pending')]
        self.app._filter()
        self.app.tree.selection_set('module:m')
        self.app._selection_changed()
        self.assertFalse(self.app.export_button.instate(['disabled']))
        with patch('telegram_archive.gui.filedialog.askdirectory',return_value=str(self.private)),patch.object(self.app,'_start') as start:
            self.app.export()
        self.assertEqual(start.call_args.args[0],'mini_export')
        self.app._mini_summary(dict(phase='media',total_bytes=597123625,done_bytes=170428620,active_bytes=1024,
            lessons=3,files_done=1,files_total=3,files_errors=0,speed_bps=1000000,pending_modules=8))
        self.app.geometry('880x690')
        self.app.update()
        self.assert_controls_fit()
        self.app._mini_summary(dict(phase='media',total_bytes=100,done_bytes=100,active_bytes=0,
            lessons=3,files_done=3,files_total=4,files_errors=0,speed_bps=100,pending_modules=8,
            unknown_sizes=True,eta_seconds=None))
        self.assertEqual(self.app.progress['value'],75)
        self.app.source_mode.set('Каналы и группы')
        self.app._source_changed()
        self.assertTrue(self.app.tree.exists('-100123'))

    def test_problem_window_retries_the_archive_owner_without_current_channel_selection(self):
        from test_archive import FakeClient, channel, message
        from telegram_archive.core import export_channel
        client = FakeClient([message(1,payload=b'video')],{10:b'video'})
        client.fail.add(10)
        with patch('telegram_archive.core.asyncio.sleep', new=AsyncMock()):
            folder = asyncio.run(export_channel(client,channel(),self.private,lambda *_:None))
        self.app.archive_path = folder
        self.app._show_issues()
        self.app.update()
        self.assertEqual(self.app.issues_data['retryable'],1)
        self.assertEqual(self.app.issues_tree.get_children(),('1',))
        self.assertFalse(self.app.retry_button.instate(['disabled']))
        self.assert_controls_fit()
        with patch.object(self.app,'_start') as start:
            self.app._retry_issues()
        self.assertEqual(start.call_args.args,('retry',))
        self.assertEqual(start.call_args.kwargs['selected']['account_id'],9)
        self.assertEqual(start.call_args.kwargs['output'],str(folder.parent))
        self.app._busy(True)
        self.assertTrue(self.app.retry_button.instate(['disabled']))

    def test_problem_window_shows_success_without_an_enabled_retry(self):
        from test_archive import FakeClient, channel, message
        from telegram_archive.core import export_channel
        folder=asyncio.run(export_channel(FakeClient([message(1,payload=b'x')],{10:b'x'}),
                                         channel(),self.private,lambda *_:None))
        self.app.archive_path=folder
        self.app._show_issues()
        self.app.update()
        self.assertEqual(self.app.issue_detail.get(),'Все файлы сохранены.')
        self.assertTrue(self.app.retry_button.instate(['disabled']))
        self.assertEqual(self.app.issues_tree.get_children(),())


if __name__ == '__main__':
    unittest.main()
