"""Shared visual language for the native Archive windows. No extra dependencies."""
import tkinter as tk
from tkinter import ttk

BG = '#0E1621'
SURFACE = '#17212B'
INK = '#E7EDF3'
MUTED = '#95A7B8'
LINE = '#2A3A4A'
GREEN = '#80B5E4'
PALE = '#223B52'
RED = '#EFABA6'
FONT = 'Helvetica Neue'


def configure_theme(root):
    root.configure(background=BG)
    root.option_add('*TCombobox*Listbox.background', SURFACE)
    root.option_add('*TCombobox*Listbox.foreground', INK)
    root.option_add('*TCombobox*Listbox.selectBackground', PALE)
    root.option_add('*TCombobox*Listbox.selectForeground', INK)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=(FONT, 12), background=BG, foreground=INK)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=SURFACE)
    style.configure('TLabel', background=BG, foreground=INK)
    style.configure('Card.TLabel', background=SURFACE, foreground=INK)
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('CardMuted.TLabel', background=SURFACE, foreground=MUTED, font=(FONT, 12))
    style.configure('Title.TLabel', font=(FONT, 22, 'bold'))
    style.configure('Section.TLabel', background=SURFACE, font=(FONT, 15, 'bold'))
    style.configure('Selected.TLabel', background=SURFACE, font=(FONT, 15, 'bold'))
    style.configure('Value.TLabel', background=SURFACE, font=('Menlo', 17), foreground=INK)
    style.configure('Badge.TLabel', background=PALE, foreground=GREEN, padding=(10, 5), font=(FONT, 11, 'bold'))
    style.configure('IdleBadge.TLabel', background=BG, foreground=MUTED, padding=(10, 5), font=(FONT, 11))
    style.configure('ErrorBadge.TLabel', background='#3A2832', foreground=RED, padding=(10, 5), font=(FONT, 11, 'bold'))
    style.configure('Error.TLabel', background=SURFACE, foreground=RED, font=(FONT, 12))
    style.configure('Step.TLabel', background=BG, foreground=MUTED, font=(FONT, 12), padding=(10, 7))
    style.configure('CurrentStep.TLabel', background=PALE, foreground=GREEN, font=(FONT, 12, 'bold'), padding=(10, 7))
    style.configure('TButton', background=SURFACE, foreground=INK, bordercolor=LINE,
                    lightcolor=SURFACE, darkcolor=SURFACE, borderwidth=1, padding=(12, 8), focusthickness=2, focuscolor=GREEN)
    style.map('TButton', background=[('disabled', BG), ('pressed', '#2C4255'), ('active', '#213243')],
              foreground=[('disabled', '#728496')], bordercolor=[('focus', GREEN)])
    style.configure('Primary.TButton', background='#4B84B9', foreground='#FFFFFF', bordercolor='#4B84B9',
                    lightcolor='#4B84B9', darkcolor='#4B84B9', font=(FONT, 12, 'bold'))
    style.map('Primary.TButton', background=[('disabled', '#243444'), ('pressed', '#3D71A3'), ('active', '#5B94C7')],
              foreground=[('disabled', '#7E91A3'), ('!disabled', '#FFFFFF')],
              bordercolor=[('disabled', '#243444'), ('focus', GREEN), ('!disabled', '#4B84B9')])
    style.configure('Quiet.TButton', background=BG, borderwidth=0, padding=(10, 8), foreground=MUTED)
    style.map('Quiet.TButton', background=[('pressed', '#233648'), ('active', '#1C2D3D')],
              foreground=[('disabled', '#5E7387'), ('active', INK)])
    style.configure('TEntry', fieldbackground='#111C27', foreground=INK, insertcolor=INK,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=(10, 9))
    style.map('TEntry', bordercolor=[('focus', GREEN)])
    style.configure('TCombobox', fieldbackground='#111C27', background=SURFACE, foreground=INK,
                    arrowcolor=MUTED, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=(10, 8))
    style.map('TCombobox', fieldbackground=[('readonly', '#111C27'), ('disabled', BG)],
              foreground=[('disabled', MUTED)], bordercolor=[('focus', GREEN)])
    style.configure('Treeview', background=SURFACE, fieldbackground='#111C27', foreground=INK,
                    rowheight=36, borderwidth=0, font=(FONT, 12))
    style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
    style.configure('Treeview.Heading', background=SURFACE, foreground=MUTED,
                    font=(FONT, 11), padding=(8, 8), relief='flat', borderwidth=0)
    style.map('Treeview', background=[('selected', PALE)], foreground=[('selected', GREEN)])
    style.map('Treeview.Heading', background=[('active', BG)])
    style.configure('Horizontal.TProgressbar', background=GREEN, troughcolor='#283A4A',
                    borderwidth=0, lightcolor=GREEN, darkcolor=GREEN, thickness=6)
    style.configure('File.Horizontal.TProgressbar', background='#4D6D8B', thickness=3)
    style.configure('Vertical.TScrollbar', background=LINE, troughcolor=SURFACE,
                    borderwidth=0, arrowsize=10, width=10)
    style.configure('TNotebook', background=SURFACE, borderwidth=0)
    style.configure('TNotebook.Tab', padding=(16, 9), background=BG)
    style.map('TNotebook.Tab', background=[('selected', SURFACE)], foreground=[('selected', GREEN)])


def panel(parent, padding=20):
    border = tk.Frame(parent, background=SURFACE, highlightbackground=LINE, highlightthickness=1, bd=0)
    body = ttk.Frame(border, padding=padding, style='Card.TFrame')
    body.pack(fill='both', expand=True)
    return border, body


def archive_mark(parent, size=42, background=BG):
    canvas = tk.Canvas(parent, width=size, height=size, bg=background, highlightthickness=0)
    # A small archive box, drawn at the current screen scale by Tk.
    canvas.create_rectangle(1, 1, size-1, size-1, fill=PALE, outline='')
    s = size / 42
    canvas.create_rectangle(11*s, 17*s, 31*s, 31*s, outline=GREEN, width=2)
    canvas.create_rectangle(9*s, 12*s, 33*s, 18*s, fill=PALE, outline=GREEN, width=2)
    canvas.create_line(18*s, 23*s, 24*s, 23*s, fill=GREEN, width=2)
    return canvas


class Progress(tk.Canvas):
    """A slim, resizable progress track independent of macOS ttk theme metrics."""
    def __init__(self, parent, height=6, length=120, foreground='#5B94C7'):
        super().__init__(parent, height=height, width=length, background='#283A4A', highlightthickness=0, bd=0)
        self.value = 0
        self.foreground = foreground
        self.bind('<Configure>', lambda _: self._paint())

    def __setitem__(self, key, value):
        if key == 'value':
            self.value = max(0, min(100, float(value)))
            self._paint()
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        return self.value if key == 'value' else super().__getitem__(key)

    def _paint(self):
        self.delete('bar')
        self.create_rectangle(0, 0, self.winfo_width() * self.value / 100,
                              self.winfo_height(), fill=self.foreground, outline='', tags='bar')
