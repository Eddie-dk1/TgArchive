"""Native macOS UI. All Tk calls run on the main thread."""
import asyncio
import fcntl
import json
import os
import queue
import re
import threading
import tkinter as tk
import webbrowser
import qrcode
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from telethon import TelegramClient, errors

from .core import atomic_text, dumps, export_channel
from .common import format_bytes, format_duration
from .issues import read_issues, PAGE_SIZE as ISSUES_PAGE_SIZE
from .paths import private_directory
from . import __version__
from .ui import configure_theme, panel, archive_mark, Progress, FONT, BG, SURFACE, INK, MUTED, LINE, PALE
from .auth import authorize_qr, session_key, forget_local_account
from .desktop import find_tdata, connection_info, saved_client, import_desktop

BASE = Path(__file__).resolve().parent.parent
PRIVATE = private_directory(BASE)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Telegram Archive')
        self.geometry('940x720')
        self.minsize(880, 690)
        self.events = queue.Queue()
        self.worker = None
        self.loop = None
        self.task = None
        self.closing = False
        self.auto_connect = True
        self.autoload_id = None
        self.qr_window = None
        self.settings_visible = False
        self.cancel_requested = threading.Event()
        self.dialogs = []
        self.visible_dialogs = []
        self.archive_path = None
        self.issues_window = None
        self.issues_data = None
        self.progress_text = tk.StringVar(value='Подключите Telegram, затем выберите канал для сохранения.')
        self.search = tk.StringVar()
        self.export_mode = tk.StringVar(value='Продолжить загрузку')
        self.overall_text = tk.StringVar(value='История и очередь файлов появятся после начала выгрузки')
        self.transfer_text = tk.StringVar(value='')
        self.file_text = tk.StringVar(value='Текущий файл: —')
        self.login_method = tk.StringVar(value='QR-код')
        self.account_text = tk.StringVar(value='Вход сохраняется на этом Mac')
        self.fields = {}
        self._build()
        self._load()
        self._filter()
        self.search.trace_add('write', lambda *_: self._filter())
        self.protocol('WM_DELETE_WINDOW', self._close)
        self.after(100, self._poll)
        try:
            desktop_saved = connection_info(PRIVATE)
        except (ValueError, OSError):
            desktop_saved = None
            self._log('Настройки сессии Desktop не читаются. Подключите Telegram Desktop повторно.')
        if self.auto_connect and self.login_method.get() == 'Telegram Desktop' and desktop_saved:
            self.autoload_id = self.after(350, self.load_channels)
        elif self.auto_connect and self.fields['api_id'].get() and self.fields['api_hash'].get():
            config = {key: value.get() for key, value in self.fields.items()}
            if (PRIVATE / (session_key(config) + '.session')).exists():
                self.autoload_id = self.after(350, self.load_channels)

    def _entry(self, parent, row, label, key, secret=False, width=28):
        ttk.Label(parent, text=label, style='Card.TLabel').grid(row=row, column=0, sticky='w', pady=7)
        ttk.Entry(parent, textvariable=self.fields[key], width=width, show='•' if secret else '').grid(
            row=row, column=1, sticky='ew', padx=(16, 0), pady=7)

    def _build(self):
        configure_theme(self)
        self.fields = {key: tk.StringVar() for key in (
            'api_id', 'api_hash', 'phone', 'proxy_host', 'proxy_port', 'proxy_user', 'proxy_password')}
        self.is_busy = False
        self.connected = False
        self.settings_window = None
        self.log_window = None
        self.current_action = None
        self.selected_title = tk.StringVar(value='Выберите канал')
        self.selected_detail = tk.StringVar(value='Нажмите на канал в списке слева.')
        self.channel_count = tk.StringVar(value='')
        self.mode_hint = tk.StringVar()
        self.phase_text = tk.StringVar(value='Готов к работе')
        self.percent_text = tk.StringVar(value='0%')
        self.metric_values = [tk.StringVar(value='—') for _ in range(3)]
        self.settings_error = tk.StringVar()
        self._log_lines = []
        outer = ttk.Frame(self, padding=(20, 18, 20, 12))
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        archive_mark(header, 36).pack(side='left', padx=(0, 14))
        title = ttk.Frame(header)
        title.pack(side='left')
        ttk.Label(title, text='Telegram Archive', style='Title.TLabel').pack(anchor='w')
        ttk.Label(title, text='Сообщения и медиа — на вашем Mac.', style='Muted.TLabel').pack(anchor='w', pady=(3, 0))
        self.settings_button = ttk.Button(header, text='Подключение', command=self._toggle_settings)
        self.settings_button.pack(side='right')
        account = ttk.Frame(header)
        account.pack(side='right', padx=(16, 16))
        self.account_badge = ttk.Label(account, text='Не подключён', style='IdleBadge.TLabel')
        self.account_badge.pack(anchor='e')
        ttk.Label(account, textvariable=self.account_text, style='Muted.TLabel', font=(FONT, 11)).pack(anchor='e', pady=(5, 0))

        steps = ttk.Frame(outer)
        steps.grid(row=1, column=0, sticky='ew', pady=(0, 12))
        self.steps = []
        for i, text in enumerate(('01   Подключите Telegram', '02   Выберите канал', '03   Сохраните архив')):
            steps.columnconfigure(i, weight=1)
            item = ttk.Label(steps, text=text, style='CurrentStep.TLabel' if i == 0 else 'Step.TLabel', anchor='center')
            item.grid(row=0, column=i, sticky='ew', padx=(0 if i == 0 else 4, 0))
            self.steps.append(item)

        workspace = ttk.Frame(outer)
        workspace.grid(row=2, column=0, sticky='nsew')
        workspace.columnconfigure(0, weight=1)
        workspace.columnconfigure(1, minsize=270)
        workspace.rowconfigure(0, weight=1)
        left_border, left = panel(workspace, 14)
        left_border.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)
        toolbar = self.channel_toolbar = ttk.Frame(left, style='Card.TFrame')
        toolbar.grid(row=0, column=0, sticky='ew')
        ttk.Label(toolbar, text='Каналы и группы', style='Section.TLabel').pack(side='left')
        ttk.Label(toolbar, textvariable=self.channel_count, style='CardMuted.TLabel').pack(side='left', padx=8)
        self.load_button = ttk.Button(toolbar, text='Обновить', command=self.load_channels, padding=(10, 6), state='disabled')
        self.load_button.pack(side='right')
        ttk.Label(left, text='Поиск по названию или ID', style='CardMuted.TLabel').grid(row=1, column=0, sticky='w', pady=(12, 5))
        self.search_entry = ttk.Entry(left, textvariable=self.search)
        self.search_entry.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        self.search_entry.bind('<Escape>', lambda _: self.search.set(''))
        self.listing = ttk.Frame(left, style='Card.TFrame')
        self.listing.grid(row=3, column=0, sticky='nsew')
        self.tree = ttk.Treeview(self.listing, columns=('kind',), show='tree headings', selectmode='browse', height=5)
        self.tree.heading('#0', text='Канал / группа', anchor='w')
        self.tree.heading('kind', text='Доступ', anchor='w')
        self.tree.column('#0', width=270, minwidth=130)
        self.tree.column('kind', width=110, minwidth=110, stretch=False)
        scroll = self.list_scroll = ttk.Scrollbar(self.listing, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.bind('<Return>', lambda _: self.export() if not self.is_busy else None)
        self.empty = ttk.Frame(self.listing, style='Card.TFrame', padding=10)
        archive_mark(self.empty, 36, SURFACE).pack(pady=(0, 8))
        self.empty_title = ttk.Label(self.empty, text='Начните с подключения', style='Section.TLabel')
        self.empty_title.pack()
        self.empty_hint = ttk.Label(self.empty, text='Используйте аккаунт из Telegram Desktop\nили войдите по QR-коду.', style='CardMuted.TLabel', justify='center')
        self.empty_hint.pack(pady=(6, 12))
        self.desktop_button = ttk.Button(self.empty, text='Подключить Telegram Desktop', command=self.connect_desktop, style='Primary.TButton')
        self.desktop_button.pack()
        self.other_login_button = ttk.Button(self.empty, text='Другой способ входа', command=self._toggle_settings, padding=(10, 6))
        self.other_login_button.pack(pady=(8, 0))
        self.empty.place(relx=.5, rely=.5, anchor='center')

        right_border, right = panel(workspace, 16)
        right_border.grid(row=0, column=1, sticky='nsew')
        right.configure(width=238)
        self.export_button = ttk.Button(right, text='Выбрать папку и начать', command=self.export, style='Primary.TButton', state='disabled')
        self.export_button.pack(side='bottom', fill='x')
        ttk.Label(right, text='Папка — на выбор при каждом запуске.\nДля продолжения выберите ту же.',
                  style='CardMuted.TLabel', wraplength=238, justify='left').pack(side='bottom', anchor='w', pady=(0, 12))
        ttk.Label(right, textvariable=self.selected_title, style='Selected.TLabel', wraplength=238).pack(anchor='w', pady=(0, 6))
        ttk.Label(right, textvariable=self.selected_detail, style='CardMuted.TLabel', wraplength=238).pack(anchor='w')
        ttk.Label(right, text='Режим сохранения', style='Card.TLabel').pack(anchor='w', pady=(14, 6))
        self.mode_box = ttk.Combobox(right, textvariable=self.export_mode,
                                     values=['Продолжить загрузку', 'Полная сверка'], state='readonly', width=23)
        self.mode_box.pack(fill='x')
        ttk.Label(right, textvariable=self.mode_hint, style='CardMuted.TLabel', wraplength=238, justify='left').pack(anchor='w', pady=(7, 12))
        self.export_mode.trace_add('write', lambda *_: self._mode_changed())
        self._mode_changed()

        status_border, status = panel(outer, (16, 12))
        status_border.grid(row=3, column=0, sticky='ew', pady=(12, 0))
        status.columnconfigure(0, weight=1)
        status.columnconfigure(1, weight=1)
        status.columnconfigure(2, weight=1)
        status.columnconfigure(3, weight=0)
        ttk.Label(status, text='Ход загрузки', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        self.phase_badge = ttk.Label(status, textvariable=self.phase_text, style='IdleBadge.TLabel')
        self.phase_badge.grid(row=0, column=1, columnspan=2, sticky='w')
        self.stop_button = ttk.Button(status, text='Остановить', command=self.stop, state='disabled', padding=(10, 6))
        self.stop_button.grid(row=0, column=3, sticky='e')
        for i, text in enumerate(('Сообщений', 'Файлов сохранено', 'Ошибок / недоступно')):
            group = ttk.Frame(status, style='Card.TFrame')
            group.grid(row=1, column=i, sticky='w', pady=(8, 8))
            ttk.Label(group, textvariable=self.metric_values[i], style='Value.TLabel').pack(anchor='w')
            ttk.Label(group, text=text, style='CardMuted.TLabel').pack(anchor='w', pady=(2, 0))
        ttk.Label(status, textvariable=self.percent_text, style='Value.TLabel').grid(row=1, column=3, sticky='e')
        self.progress = Progress(status)
        self.progress.grid(row=2, column=0, columnspan=4, sticky='ew')
        ttk.Label(status, textvariable=self.progress_text, style='Card.TLabel', wraplength=840).grid(row=3, column=0, columnspan=4, sticky='w', pady=(8, 2))
        ttk.Label(status, textvariable=self.transfer_text, style='CardMuted.TLabel').grid(row=4, column=0, columnspan=4, sticky='w')
        self.file_line = ttk.Frame(status, style='Card.TFrame')
        self.file_line.grid(row=5, column=0, columnspan=4, sticky='ew', pady=(8, 0))
        ttk.Label(self.file_line, textvariable=self.file_text, style='CardMuted.TLabel').pack(side='left')
        self.file_progress = Progress(self.file_line, height=3, length=110, foreground='#4D6D8B')
        self.file_progress.pack(side='right')
        self.file_line.grid_remove()

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky='ew', pady=(10, 0))
        ttk.Label(footer, text='Очередь сохраняется при остановке.', style='Muted.TLabel', font=(FONT, 11)).pack(side='left')
        self.open_button = ttk.Button(footer, text='Открыть архив', state='disabled', command=self.open_archive, style='Quiet.TButton')
        self.open_button.pack(side='right')
        self.report_button = ttk.Button(footer, text='Проблемные файлы', command=self._show_issues, style='Quiet.TButton')
        self.report_button.pack(side='right', padx=4)
        ttk.Button(footer, text='Журнал', command=self._show_log, style='Quiet.TButton').pack(side='right')
        self.bind('<Command-f>', self._focus_search)
        self.bind('<Command-comma>', lambda _: self._toggle_settings())

    def _focus_search(self, _event=None):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, 'end')
        return 'break'

    def _mode_changed(self):
        if self.export_mode.get() == 'Полная сверка':
            self.mode_hint.set('Проверит всю историю и обновит посты. Скачанные файлы использует повторно.')
        else:
            self.mode_hint.set('Новый архив — вся история. Существующий — продолжение загрузки файлов.')

    def _selection_changed(self, _event=None):
        selection = self.tree.selection()
        selected = next((d for d in self.visible_dialogs if selection and str(d['id']) == selection[0]), None)
        if selected:
            self.selected_title.set(selected['title'][:49] + ('…' if len(selected['title']) > 49 else ''))
            access = 'Открытый' if selected['public'] else 'Приватный'
            self.selected_detail.set(f'{access} · ID {selected["id"]}' +
                                     ('\nВключена защита контента' if selected['protected'] else ''))
        else:
            self.selected_title.set('Выберите канал')
            self.selected_detail.set('Нажмите на канал в списке слева.')
        self.export_button.configure(state='normal' if selected and not self.is_busy else 'disabled')
        active = 2 if selected else 1 if self.connected else 0
        for i, step in enumerate(self.steps):
            step.configure(style='CurrentStep.TLabel' if i == active else 'Step.TLabel')

    def _toggle_settings(self):
        if self.settings_visible:
            self._close_settings()
            return
        if self.is_busy:
            return
        self.settings_visible = True
        window = self.settings_window = tk.Toplevel(self, background=SURFACE)
        window.title('Подключение к Telegram')
        window.geometry('590x590')
        window.resizable(False, False)
        window.transient(self)
        window.protocol('WM_DELETE_WINDOW', self._close_settings)
        window.bind('<Escape>', lambda _: self._close_settings())
        body = ttk.Frame(window, style='Card.TFrame', padding=22)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Подключение к Telegram', style='Selected.TLabel').pack(anchor='w')
        ttk.Label(body, text='Войдите один раз. Следующий запуск — без повторного входа.', style='CardMuted.TLabel').pack(anchor='w', pady=(7, 18))
        tabs = ttk.Notebook(body)
        tabs.pack(fill='both', expand=True)
        auth = ttk.Frame(tabs, style='Card.TFrame', padding=(4, 18))
        proxy = ttk.Frame(tabs, style='Card.TFrame', padding=(4, 18))
        tabs.add(auth, text='Аккаунт')
        tabs.add(proxy, text='Прокси SOCKS5')
        row = ttk.Frame(auth, style='Card.TFrame')
        row.pack(fill='x')
        ttk.Label(row, text='Способ входа', style='Card.TLabel').pack(side='left')
        method = ttk.Combobox(row, textvariable=self.login_method, values=['Telegram Desktop', 'QR-код', 'Номер телефона'], state='readonly', width=26)
        method.pack(side='right')
        desktop = ttk.Frame(auth, style='Card.TFrame')
        api = ttk.Frame(auth, style='Card.TFrame')
        ttk.Label(desktop, text='Используйте аккаунт, открытый в Telegram Desktop.', style='Section.TLabel', wraplength=510).pack(anchor='w', pady=(20, 10))
        ttk.Label(desktop, text='Приложение найдёт локальные данные Desktop.\nНомер телефона, API ID и API Hash вводить не нужно.\n\nЗакрытие Archive не завершает сеанс Desktop.\n«Выйти из Archive» удаляет только локальный вход Archive.', style='CardMuted.TLabel', justify='left', wraplength=510).pack(anchor='w')
        self._entry(api, 0, 'API ID', 'api_id')
        self._entry(api, 1, 'API Hash', 'api_hash', secret=True)
        self._entry(api, 2, 'Телефон (для входа по номеру)', 'phone')
        api.columnconfigure(1, weight=1)
        ttk.Button(api, text='Получить API ID и Hash', command=lambda: webbrowser.open('https://my.telegram.org/apps')).grid(row=3, column=0, columnspan=2, sticky='w', pady=(12, 8))
        ttk.Label(api, text='Данные приложения нужны только при первом подключении.\nДля QR-кода номер телефона не требуется.', style='CardMuted.TLabel', justify='left').grid(row=4, column=0, columnspan=2, sticky='w', pady=8)
        def show_method(_event=None):
            desktop.pack_forget()
            api.pack_forget()
            (desktop if self.login_method.get() == 'Telegram Desktop' else api).pack(fill='x')
        method.bind('<<ComboboxSelected>>', show_method)
        show_method()
        ttk.Label(proxy, text='Необязательно. Используйте, если Telegram\nнедоступен через ваше обычное соединение.', style='CardMuted.TLabel').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 14))
        for row, (label, key, secret) in enumerate((('Сервер', 'proxy_host', False), ('Порт', 'proxy_port', False), ('Логин', 'proxy_user', False), ('Пароль', 'proxy_password', True)), 1):
            self._entry(proxy, row, label, key, secret=secret)
        proxy.columnconfigure(1, weight=1)
        ttk.Label(body, textvariable=self.settings_error, style='Error.TLabel', wraplength=550).pack(anchor='w', pady=(10, 8))
        actions = ttk.Frame(body, style='Card.TFrame')
        actions.pack(fill='x')
        self.forget_button = ttk.Button(actions, text='Выйти из Archive', command=self.forget_account)
        self.forget_button.pack(side='left')
        ttk.Button(actions, text='Подключить', style='Primary.TButton', command=self._connect_from_settings).pack(side='right')
        ttk.Button(actions, text='Готово', command=self._save_settings).pack(side='right', padx=8)
        window.lift()

    def _save_settings(self):
        try:
            self._settings(importing_desktop=self.login_method.get() == 'Telegram Desktop')
        except (ValueError, OSError) as exc:
            self.settings_error.set(str(exc))
            return
        self.settings_error.set('')
        self._close_settings()

    def _connect_from_settings(self):
        if self.login_method.get() == 'Telegram Desktop' and not connection_info(PRIVATE):
            self.connect_desktop()
        else:
            self.load_channels()

    def _close_settings(self):
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None
        self.settings_visible = False

    def _show_log(self):
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        self.log_window = window = tk.Toplevel(self, background=BG)
        window.title('Журнал · Telegram Archive')
        window.geometry('820x420')
        window.transient(self)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Журнал приложения', font=(FONT, 18, 'bold')).pack(anchor='w', pady=(0, 12))
        ttk.Label(frame, text='Здесь — этапы загрузки и подробности ошибок.', style='Muted.TLabel').pack(anchor='w', pady=(0, 12))
        self.log = tk.Text(frame, wrap='word', font=('Menlo', 11), background=SURFACE, foreground=INK,
                           selectbackground=PALE, selectforeground=INK, highlightbackground=LINE,
                           highlightthickness=1, bd=0, padx=14, pady=12)
        scroll = ttk.Scrollbar(frame, command=self.log.yview)
        scroll.pack(side='right', fill='y')
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(fill='both', expand=True)
        self.log.insert('end', '\n'.join(self._log_lines))
        self.log.configure(state='disabled')
        self.log.see('end')
        window.bind('<Escape>', lambda _: window.destroy())

    def _load(self):
        path = PRIVATE / 'settings.json'
        if not path.exists():
            return
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
            for key, value in self.fields.items():
                value.set(config.get(key, ''))
            self.login_method.set(config.get('login_method', 'QR-код'))
            self.export_mode.set(config.get('export_mode', 'Продолжить загрузку'))
            self.auto_connect = bool(config.get('auto_connect', True))
        except (ValueError, OSError):
            self._log('Настройки не удалось прочитать. Введите их заново.')

    def _settings(self, importing_desktop=False):
        config = {key: var.get().strip() for key, var in self.fields.items()}
        config['proxy_password'] = self.fields['proxy_password'].get()
        config['login_method'] = self.login_method.get()
        config['export_mode'] = self.export_mode.get()
        desktop_mode = config['login_method'] == 'Telegram Desktop'
        if desktop_mode and not importing_desktop and not connection_info(PRIVATE):
            raise ValueError('Сначала нажмите «Подключить Telegram Desktop».')
        if not desktop_mode and (not config['api_id'].isdigit() or int(config['api_id']) <= 0):
            raise ValueError('API ID — положительное число с my.telegram.org/apps.')
        if not desktop_mode and not re.fullmatch('[a-fA-F0-9]{32}', config['api_hash']):
            raise ValueError('API Hash должен содержать 32 шестнадцатеричных символа.')
        phone = re.sub(r'[\s()-]', '', config['phone'])
        if config['login_method'] == 'Номер телефона' and not re.fullmatch(r'\+\d{8,15}', phone):
            raise ValueError('Введите телефон в международном формате: +79991234567.')
        config['phone'] = phone
        if config['proxy_host'] and (not config['proxy_port'].isdigit() or
                                     not 0 < int(config['proxy_port']) < 65536):
            raise ValueError('Укажите порт SOCKS5 от 1 до 65535.')
        atomic_text(PRIVATE / 'settings.json', dumps(config))
        (PRIVATE / 'settings.json').chmod(0o600)
        return config

    def _busy(self, busy):
        self.is_busy = busy
        if busy:
            self._close_settings()
        for button in (self.load_button, self.settings_button, self.desktop_button, self.other_login_button):
            button.configure(state='disabled' if busy else 'normal')
        self.load_button.configure(state='normal' if self.connected and not busy else 'disabled')
        self.stop_button.configure(state='normal' if busy else 'disabled')
        self.mode_box.configure(state='disabled' if busy else 'readonly')
        self._issue_actions()
        self._selection_changed()

    def emit(self, kind, value):
        self.events.put((kind, value))

    async def _prompt(self, title, text, secret=False):
        future = asyncio.get_running_loop().create_future()
        self.emit('prompt', (title, text, secret, future, asyncio.get_running_loop()))
        value = await future
        if value is None:
            raise asyncio.CancelledError()
        return value.strip() if not secret else value

    async def _connect(self, config, action, selected=None, output=None):
        token = session_key(config)
        kwargs = {}
        if config['proxy_host']:
            kwargs['proxy'] = dict(proxy_type='socks5', addr=config['proxy_host'],
                                   port=int(config['proxy_port']), rdns=True,
                                   username=config['proxy_user'] or None,
                                   password=config['proxy_password'] or None)
        client = None
        async def code():
            return await self._prompt('Код Telegram', 'Введите код входа, который прислал Telegram:')
        async def password():
            return await self._prompt('Пароль 2FA', 'Введите облачный пароль Telegram:', True)
        try:
            self.emit('status', 'Подключение к Telegram…')
            if action == 'desktop':
                client = await import_desktop(output, PRIVATE, self._prompt, self.emit,
                                              flood_sleep_threshold=0, receive_updates=False, **kwargs)
                me = await client.get_me()
            elif config['login_method'] == 'Telegram Desktop':
                client = saved_client(PRIVATE, connection_info(PRIVATE), flood_sleep_threshold=0,
                                      receive_updates=False, **kwargs)
                await client.connect()
                if not await client.is_user_authorized():
                    raise ValueError('Сессия Desktop больше не действует. Нажмите «Подключить Telegram Desktop».')
                me = await client.get_me()
            else:
                client = TelegramClient(str(PRIVATE / token), int(config['api_id']), config['api_hash'],
                                        device_model='Mac — Telegram Archive', app_version=__version__,
                                        flood_sleep_threshold=0, receive_updates=True, **kwargs)
            if config['login_method'] == 'Номер телефона':
                await client.start(phone=config['phone'], code_callback=code, password=password)
                me = await client.get_me()
            elif config['login_method'] == 'QR-код':
                me = await authorize_qr(client, self.emit, self._prompt)
            await client.set_receive_updates(False)
            self.emit('account', (me.first_name or 'Telegram', me.id))
            if selected and selected['account_id'] != me.id:
                raise ValueError('Аккаунт изменён. Сначала обновите список каналов.')
            if action in ('list', 'desktop'):
                self.emit('status', 'Читаю список ваших каналов и групп…')
                dialogs = []
                async for dialog in client.iter_dialogs():
                    entity = dialog.entity
                    if dialog.is_channel or dialog.is_group:
                        if getattr(entity, 'left', False) or getattr(entity, 'deactivated', False):
                            continue
                        dialogs.append({'id': dialog.id, 'title': dialog.name or str(dialog.id),
                                        'public': bool(getattr(entity, 'username', None)),
                                        'protected': bool(getattr(entity, 'noforwards', False)),
                                        'account_id': me.id})
                self.emit('dialogs', dialogs)
                self.emit('status', f'Найдено каналов и групп: {len(dialogs)}. Выберите один и папку.')
            else:
                entity = await client.get_entity(selected['id'])
                if getattr(entity, 'noforwards', False):
                    self.emit('log', 'В канале включена защита контента. Читаю доступные посты через API.')
                mode = 'retry' if action == 'retry' else 'full' if config['export_mode'] == 'Полная сверка' else 'resume'
                await export_channel(client, entity, output, self.emit, mode=mode)
        finally:
            if client is not None:
                await client.disconnect()
            for path in PRIVATE.glob('*.session*'):
                path.chmod(0o600)

    def _start(self, action, selected=None, output=None):
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._settings(importing_desktop=action == 'desktop')
        except (ValueError, OSError) as exc:
            if not self.settings_visible:
                self._toggle_settings()
            self.settings_error.set(str(exc))
            return
        self.current_action = action
        self._busy(True)
        self.phase_text.set('Подключение' if action not in ('export','retry') else 'Подготовка')
        self.phase_badge.configure(style='Badge.TLabel')
        self.progress_text.set('Подключение к Telegram…')
        self.percent_text.set('0%')
        if action in ('export', 'retry'):
            for value in self.metric_values:
                value.set('—')
        self.cancel_requested.clear()
        self.progress['value'] = 0
        self.file_progress['value'] = 0
        self.transfer_text.set('')
        self.file_text.set('Текущий файл: —')
        self._log('Подключение Telegram Desktop…' if action == 'desktop' else
                  'Загрузка каналов…' if action == 'list' else f'Выгрузка: {selected["title"]}\nПапка: {output}')
        def run():
            async def main():
                self.loop = asyncio.get_running_loop()
                self.task = asyncio.current_task()
                if self.cancel_requested.is_set():
                    raise asyncio.CancelledError()
                await self._connect(config, action, selected, output)
            try:
                asyncio.run(main())
            except asyncio.CancelledError:
                self.emit('log', 'Остановлено. Сохранённые сообщения и части файлов остаются для продолжения.')
                self.emit('status', 'Остановлено — можно продолжить, выбрав ту же папку')
            except Exception as exc:
                if isinstance(exc, errors.FloodWaitError):
                    message = f'Telegram просит подождать {exc.seconds} с. Повторите действие позже.'
                elif isinstance(exc, (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError)):
                    message = 'Код неверный или истёк. Повторите вход.'
                elif isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
                    message = f'Проверьте сеть, VPN / SOCKS5 и доступность папки. {type(exc).__name__}: {exc}'
                else:
                    message = f'{type(exc).__name__}: {exc}'
                self.emit('log', message)
                self.emit('status', 'Действие не завершено — подробности в журнале')
                self.emit('error', message)
            finally:
                self.loop = None
                self.task = None
                self.emit('done', None)
        self.worker = threading.Thread(target=run, daemon=False)
        self.worker.start()

    def load_channels(self):
        self.autoload_id = None
        self._start('list')

    def forget_account(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo('Остановите загрузку', 'Дождитесь завершения или остановите загрузку перед выходом.', parent=self)
            return
        if self.autoload_id is not None:
            self.after_cancel(self.autoload_id)
            self.autoload_id = None
        try:
            forget_local_account(PRIVATE)
        except OSError as exc:
            messagebox.showerror('Выход не завершён',
                                 f'Не удалось удалить сохранённую авторизацию: {exc}. Повторите выход.', parent=self)
            return
        self.auto_connect = False
        self.fields['phone'].set('')
        self.login_method.set('QR-код')
        self.dialogs = []
        self.connected = False
        self.account_badge.configure(text='Не подключён', style='IdleBadge.TLabel')
        self.phase_text.set('Готов к работе')
        self.percent_text.set('0%')
        for value in self.metric_values:
            value.set('—')
        self.search.set('')
        self._filter()
        self._close_qr()
        self.progress['value'] = 0
        self.file_progress['value'] = 0
        self.overall_text.set('Вход в Archive удалён')
        self.transfer_text.set('')
        self.file_text.set('Текущий файл: —')
        self.account_text.set('Вход удалён только из Archive')
        self.progress_text.set('Вы вышли из Archive. Telegram Desktop остаётся в аккаунте.')
        self.load_button.configure(text='Обновить')
        self._log_lines.clear()
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log.configure(state='normal')
            self.log.delete('1.0', 'end')
            self.log.configure(state='disabled')
        self._close_settings()
        self._log('Локальная авторизация Archive удалена. Сохранённые архивы остались на диске.')

    def connect_desktop(self):
        paths = find_tdata()
        path = paths[0] if paths else filedialog.askdirectory(
            parent=self, title='Выберите папку tdata Telegram Desktop', mustexist=True)
        if path:
            self.login_method.set('Telegram Desktop')
            self._start('desktop', output=str(path))

    def export(self):
        if self.is_busy:
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo('Выберите канал', 'Загрузите список каналов и выберите один.', parent=self)
            return
        selected = next(d for d in self.visible_dialogs if str(d['id']) == selection[0])
        output = filedialog.askdirectory(parent=self, title='Папка для архива канала', mustexist=True)
        if output:
            self._start('export', selected, output)

    def stop(self):
        self.cancel_requested.set()
        loop, task = self.loop, self.task
        if loop and task and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        self.stop_button.configure(state='disabled')

    def _show_qr(self, url):
        if self.cancel_requested.is_set() or self.closing:
            return
        if self.qr_window is None or not self.qr_window.winfo_exists():
            self.qr_window = tk.Toplevel(self, background=BG)
            self.qr_window.title('Вход в Telegram по QR-коду')
            self.qr_window.resizable(False, False)
            self.qr_window.transient(self)
            self.qr_window.protocol('WM_DELETE_WINDOW', self.stop)
            frame = ttk.Frame(self.qr_window, padding=24)
            frame.pack()
            ttk.Label(frame, text='Войдите через Telegram на телефоне',
                      font=(FONT, 18, 'bold')).pack()
            ttk.Label(frame, text='Настройки → Устройства → Подключить устройство\n'
                      'Отсканируйте QR-код и подтвердите вход.', justify='center').pack(pady=12)
            self.qr_canvas = tk.Canvas(frame, bg='white', highlightthickness=0)
            self.qr_canvas.pack()
            ttk.Label(frame, text='Код обновляется автоматически.\nВ следующий раз вход будет автоматическим.',
                      justify='center').pack(pady=12)
            ttk.Button(frame, text='Отмена', command=self.stop).pack()
        qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        scale = max(4, 300 // len(matrix))
        self.qr_canvas.configure(width=len(matrix) * scale, height=len(matrix) * scale)
        self.qr_canvas.delete('all')
        for y, row in enumerate(matrix):
            for x, black in enumerate(row):
                if black:
                    self.qr_canvas.create_rectangle(x * scale, y * scale, (x + 1) * scale,
                                                    (y + 1) * scale, fill='black', outline='')
        self.qr_window.lift()

    def _close_qr(self):
        if self.qr_window is not None and self.qr_window.winfo_exists():
            self.qr_window.destroy()
        self.qr_window = None

    def _filter(self):
        selection = self.tree.selection()
        query = self.search.get().casefold().strip()
        self.visible_dialogs = [d for d in self.dialogs if query in d['title'].casefold() or query in str(d['id'])]
        self.tree.delete(*self.tree.get_children())
        for d in self.visible_dialogs:
            access = 'Открытый' if d['public'] else 'Приватный'
            self.tree.insert('', 'end', iid=str(d['id']), text='  ' + d['title'], values=(access,))
        self.channel_count.set(f'{len(self.visible_dialogs)} / {len(self.dialogs)}' if query else str(len(self.dialogs)) if self.dialogs else '')
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection[0])
        if self.visible_dialogs:
            self.empty.place_forget()
            self.tree.pack(side='left', fill='both', expand=True)
            self.list_scroll.pack(side='right', fill='y')
        else:
            self.tree.pack_forget()
            self.list_scroll.pack_forget()
            if query:
                self.empty_title.configure(text='Ничего не найдено')
                self.empty_hint.configure(text='Попробуйте другое название или ID.\nEsc — очистить поиск.')
            elif self.connected:
                self.empty_title.configure(text='Список пока пуст')
                self.empty_hint.configure(text='Нажмите «Обновить», чтобы загрузить\nваши каналы и группы.')
            else:
                self.empty_title.configure(text='Начните с подключения')
                self.empty_hint.configure(text='Используйте аккаунт из Telegram Desktop\nили войдите по QR-коду.')
            if query or self.connected:
                self.desktop_button.pack_forget()
                self.other_login_button.pack_forget()
            else:
                self.desktop_button.pack()
                self.other_login_button.pack(pady=(8, 0))
            self.empty.place(relx=.5, rely=.5, anchor='center')
        self._selection_changed()

    def _log(self, text):
        self._log_lines.extend(str(text).splitlines())
        self._log_lines = self._log_lines[-600:]
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log.configure(state='normal')
            self.log.delete('1.0', 'end')
            self.log.insert('end', '\n'.join(self._log_lines) + '\n')
            self.log.see('end')
            self.log.configure(state='disabled')

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self._log(value)
                elif kind == 'status':
                    self.progress_text.set(value)
                elif kind == 'dialogs':
                    self.dialogs = sorted(value, key=lambda d: d['title'].casefold())
                    self._filter()
                elif kind == 'qr':
                    self._show_qr(value)
                elif kind == 'qr_close':
                    self._close_qr()
                elif kind == 'account':
                    name, uid = value
                    self.connected = True
                    self.account_badge.configure(text=name, style='Badge.TLabel')
                    self.account_text.set(f'ID {uid} · вход сохранён')
                    self.load_button.configure(text='Обновить')
                    if self.settings_visible:
                        self._toggle_settings()
                elif kind == 'summary':
                    self._summary(value)
                elif kind == 'progress':
                    mid, received, total = value
                    self.file_progress['value'] = received * 100 / total if total else 0
                    self.file_text.set(f'Файл #{mid}: {format_bytes(received)}' +
                                       (f' / {format_bytes(total)}' if total else ''))
                elif kind == 'archive':
                    self.archive_path = Path(value)
                    self.open_button.configure(state='normal')
                    self.report_button.configure(state='normal')
                elif kind == 'issues_changed':
                    if self.issues_data and self.issues_data['root'] == Path(value):
                        self._reload_issues(self.issues_data['offset'])
                elif kind == 'error' and not self.closing:
                    self.phase_text.set('Не завершено')
                    self.phase_badge.configure(style='ErrorBadge.TLabel')
                    messagebox.showerror('Не удалось завершить', value, parent=self)
                elif kind == 'prompt':
                    title, text, secret, future, loop = value
                    if not future.done():
                        answer = simpledialog.askstring(title, text, show='•' if secret else None, parent=self)
                        def resolve(f=future, a=answer):
                            if not f.done():
                                f.set_result(a)
                        if not loop.is_closed():
                            loop.call_soon_threadsafe(resolve)
                elif kind == 'done':
                    self._close_qr()
                    self._busy(False)
                    if self.current_action in ('list', 'desktop') and self.phase_text.get() != 'Не завершено':
                        self.phase_text.set('Выберите канал' if self.connected else 'Остановлено')
                    self.current_action = None
                    if self.closing:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _summary(self, data):
        if data['phase'] == 'history':
            hint = data.get('history_total_hint')
            self.overall_text.set(f'История: собрано {data["history_seen"]}' +
                                  (f' из примерно {hint} сообщений' if hint else ' сообщений'))
            self.progress['value'] = min(99, data['history_seen'] * 100 / hint) if hint else 0
        else:
            self.overall_text.set(f'История: {data["history_seen"]} сообщений · '
                                  f'Файлы: {data["files_done"]}/{data["files_total"]} · '
                                  f'Осталось: {data["files_pending"]} · '
                                  f'Ошибок: {data["files_errors"]} · Недоступно: {data["files_unavailable"]}')
            total = data['total_bytes']
            active = data['active_bytes'] if data.get('active_id') is not None else 0
            self.progress['value'] = min(100, (data['done_bytes'] + active) * 100 / total) if total else (
                data['files_done'] * 100 / data['files_total'] if data['files_total'] else 0)
        speed = 'пауза Telegram' if data['waiting'] else f'{format_bytes(data["speed_bps"])}/с'
        eta = format_duration(data['eta_seconds']) if data['phase'] == 'media' and not data['waiting'] else '—'
        self.transfer_text.set(f'Завершено {format_bytes(data["done_bytes"])} / {format_bytes(data["total_bytes"])} · '
                               f'{speed} · оценка времени: {eta} · '
                               f'прошло {format_duration(data["elapsed_seconds"])}')
        if hasattr(self, 'metric_values'):
            self.metric_values[0].set(f'{data["history_seen"]:,}'.replace(',', ' '))
            self.metric_values[1].set(f'{data["files_done"]}/{data["files_total"]}')
            self.metric_values[2].set(f'{data["files_errors"]} / {data["files_unavailable"]}')
            phase = data['phase']
            titles = {'prepare': 'Подготовка', 'history': 'Сохраняем сообщения', 'export': 'Создаём архив',
                      'gaps': 'Проверяем пропуски', 'verify': 'Проверяем SHA-256', 'media': 'Скачиваем файлы',
                      'cancelled': 'Остановлено · можно продолжить', 'finished': 'Завершено'}
            problems = data['files_errors'] + data['files_unavailable']
            title = 'Завершено с пропусками' if phase == 'finished' and problems else titles.get(phase, 'Загрузка')
            self.phase_text.set('Ожидание Telegram' if data['waiting'] else title)
            self.phase_badge.configure(style='ErrorBadge.TLabel' if phase == 'finished' and problems else 'Badge.TLabel')
            self.percent_text.set(f'{self.progress["value"]:.0f}%')
            if phase == 'media':
                self.file_line.grid()
            else:
                self.file_line.grid_remove()
        if data['phase'] in ('finished', 'cancelled'):
            self.file_text.set('Текущий файл: —')
            self.file_progress['value'] = 0

    def _show_issues(self):
        if self.issues_window is not None and self.issues_window.winfo_exists():
            self._reload_issues(self.issues_data['offset'])
            self.issues_window.lift()
            return
        folder = self.archive_path or filedialog.askdirectory(parent=self,
            title='Выберите папку channel_<ID> сохранённого архива', mustexist=True)
        if not folder:
            return
        try:
            self.issues_data = read_issues(folder)
        except Exception as exc:
            messagebox.showerror('Не удалось открыть архив', str(exc), parent=self)
            return
        self.issues_window = window = tk.Toplevel(self, background=BG)
        window.title('Проблемные файлы · Telegram Archive')
        window.geometry('900x530')
        window.minsize(880, 530)
        window.transient(self)
        window.bind('<Escape>', lambda _: window.destroy())
        body = ttk.Frame(window, padding=20)
        body.pack(fill='both', expand=True)
        self.issues_heading = tk.StringVar()
        self.issues_caption = tk.StringVar()
        self.issue_detail = tk.StringVar(value='Выберите строку, чтобы увидеть причину целиком.')
        ttk.Label(body, textvariable=self.issues_heading, font=(FONT, 18, 'bold')).pack(anchor='w')
        ttk.Label(body, textvariable=self.issues_caption, style='Muted.TLabel').pack(anchor='w', pady=(7, 12))
        listing = ttk.Frame(body)
        listing.pack(fill='both', expand=True)
        self.issues_tree = ttk.Treeview(listing, columns=('status','attempts','reason'), show='tree headings', selectmode='browse', height=6)
        for column, title, width in (('#0','Пост',90), ('status','Состояние',160), ('attempts','Попыток',70), ('reason','Причина',450)):
            self.issues_tree.heading(column, text=title, anchor='w')
            self.issues_tree.column(column, width=width, minwidth=50, stretch=column=='reason')
        scroll = ttk.Scrollbar(listing, orient='vertical', command=self.issues_tree.yview)
        self.issues_tree.configure(yscrollcommand=scroll.set)
        self.issues_tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.issues_tree.bind('<<TreeviewSelect>>', self._issue_selected)
        ttk.Label(body, textvariable=self.issue_detail, style='Muted.TLabel', wraplength=840).pack(anchor='w', pady=(12, 8))
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(4, 0))
        self.retry_button = ttk.Button(actions, text='Повторить проблемные', style='Primary.TButton', command=self._retry_issues)
        self.retry_button.pack(side='left')
        self.issue_open_button = ttk.Button(actions, text='Открыть пост', command=self._open_issue_post, state='disabled')
        self.issue_open_button.pack(side='left', padx=8)
        ttk.Button(actions, text='HTML-отчёт', command=self._open_issues_report).pack(side='left')
        self.issues_next = ttk.Button(actions, text='Далее', command=lambda: self._reload_issues(self.issues_data['offset']+ISSUES_PAGE_SIZE))
        self.issues_next.pack(side='right')
        self.issues_previous = ttk.Button(actions, text='Назад', command=lambda: self._reload_issues(self.issues_data['offset']-ISSUES_PAGE_SIZE))
        self.issues_previous.pack(side='right', padx=8)
        bottom = ttk.Frame(body)
        bottom.pack(fill='x', pady=(10,0))
        ttk.Label(bottom, text='Повтор затрагивает ошибки и недоступные вложения. Ожидающие файлы — через «Продолжить загрузку».',
                  style='Muted.TLabel', font=(FONT, 11), wraplength=600).pack(side='left')
        ttk.Button(bottom, text='Другой архив', command=self._choose_issues_archive, style='Quiet.TButton').pack(side='right')
        self._reload_issues(0)

    def _choose_issues_archive(self):
        folder = filedialog.askdirectory(parent=self.issues_window, title='Папка channel_<ID>', mustexist=True)
        if folder:
            self._reload_issues(0, folder)

    def _reload_issues(self, offset=0, folder=None):
        if self.issues_window is None or not self.issues_window.winfo_exists():
            return
        try:
            self.issues_data = read_issues(folder or self.issues_data['root'], offset)
        except Exception as exc:
            messagebox.showerror('Не удалось прочитать архив', str(exc), parent=self.issues_window)
            return
        data = self.issues_data
        self.issues_heading.set(data['title'][:80])
        start = data['offset']+1 if data['total'] else 0
        self.issues_caption.set(f'Не сохранено: {data["total"]} · можно повторить: {data["retryable"]} · показано {start}–{data["offset"]+len(data["rows"])}')
        self.issues_tree.delete(*self.issues_tree.get_children())
        labels = {'error':'Ошибка', 'unavailable':'Недоступно в Telegram', 'pending':'Ожидает загрузки', 'downloading':'Загружается'}
        for row in data['rows']:
            self.issues_tree.insert('', 'end', iid=str(row['id']), text=f'#{row["id"]}',
                values=(labels.get(row['status'],row['status']),row['attempts'],row['error'] or 'Ещё не скачан'))
        self.issue_detail.set('Все файлы сохранены.' if not data['total'] else 'Выберите строку, чтобы увидеть причину целиком.')
        self.issue_open_button.configure(state='disabled')
        self.issues_previous.configure(state='normal' if data['offset'] else 'disabled')
        self.issues_next.configure(state='normal' if data['offset']+ISSUES_PAGE_SIZE < data['total'] else 'disabled')
        self._issue_actions()

    def _issue_actions(self):
        if self.issues_window is not None and self.issues_window.winfo_exists() and hasattr(self, 'retry_button'):
            self.retry_button.configure(state='normal' if self.issues_data['retryable'] and not self.is_busy else 'disabled')

    def _issue_selected(self, _event=None):
        selection = self.issues_tree.selection()
        row = next((r for r in self.issues_data['rows'] if selection and str(r['id'])==selection[0]), None)
        if row:
            reason = row['error'] or 'Файл ещё не скачан. Используйте продолжение загрузки.'
            self.issue_detail.set(reason[:450] + ('… Полный текст — в HTML-отчёте.' if len(reason)>450 else ''))
            self.issue_open_button.configure(state='normal' if (row['link'] or '').startswith('https://t.me/') else 'disabled')

    def _open_issue_post(self):
        selection = self.issues_tree.selection()
        row = next((r for r in self.issues_data['rows'] if selection and str(r['id'])==selection[0]), None)
        if row and (row['link'] or '').startswith('https://t.me/'):
            webbrowser.open(row['link'])

    def _open_issues_report(self):
        path = self.issues_data['root'] / 'report.html'
        if path.exists():
            webbrowser.open(path.as_uri())

    def _retry_issues(self):
        if self.is_busy:
            return
        self._reload_issues(self.issues_data['offset'])
        data = self.issues_data
        if not data['retryable']:
            return
        selected = {key: data[key] for key in ('id','account_id','title')}
        self._start('retry', selected=selected, output=str(data['root'].parent))

    def open_report(self):
        if self.archive_path and (self.archive_path / 'report.html').exists():
            webbrowser.open((self.archive_path / 'report.html').as_uri())

    def open_archive(self):
        if self.archive_path and (self.archive_path / 'index.html').exists():
            webbrowser.open((self.archive_path / 'index.html').as_uri())

    def _close(self):
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.stop()
            self.progress_text.set('Сохраняю архив перед закрытием…')
        else:
            self.destroy()


def main():
    os.umask(0o077)
    PRIVATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    PRIVATE.chmod(0o700)
    # Keep the descriptor open for the entire process; never unlink the lock file.
    lock = (PRIVATE / 'app.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo('Telegram Archive', 'Приложение уже запущено.')
        root.destroy()
        return
    App().mainloop()


if __name__ == '__main__':
    main()
