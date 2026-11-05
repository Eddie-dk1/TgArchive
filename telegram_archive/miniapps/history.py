"""Persistent, credential-free source history, independent of downloaded files."""
import json
import sqlite3
from pathlib import Path
from ..common import now
from ..paths import private_directory


def default_history_path():
    return private_directory(Path(__file__).resolve().parents[2]) / 'history' / 'miniapps.sqlite3'


class History:
    def __init__(self, path, source, account, bot):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.scope = json.dumps([source, account, bot])
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS nodes(scope TEXT,id TEXT,title TEXT,kind TEXT,parent TEXT,
            PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS versions(scope TEXT,id TEXT,node TEXT,label TEXT,size INTEGER,
            sha256 TEXT,status TEXT,first_seen TEXT,last_seen TEXT,PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS latest(scope TEXT,node TEXT,label TEXT,id TEXT,
            PRIMARY KEY(scope,node,label));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,date TEXT,scope TEXT,node TEXT,
            title TEXT,label TEXT,change TEXT,old_id TEXT,new_id TEXT,detail TEXT);
        ''')
        path.chmod(0o600)

    def observe(self, catalogue):
        changes = []
        titles = {n.id:n.title for n in catalogue.nodes}
        with self.db:
            known = {r[0] for r in self.db.execute('SELECT id FROM nodes WHERE scope=?',(self.scope,))}
            for n in catalogue.nodes:
                self.db.execute('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?)',
                    (self.scope,n.id,n.title,n.kind,n.parent))
            for a in catalogue.assets:
                prior = self.db.execute('SELECT id FROM latest WHERE scope=? AND node=? AND label=?',
                                        (self.scope,a.node,a.label)).fetchone()
                old = prior[0] if prior else None
                change = ('new_lesson' if a.node not in known else 'new_attachment') if old is None else (
                    'source_changed' if old != a.id else 'unchanged')
                stamp = now()
                self.db.execute('''INSERT INTO versions VALUES(?,?,?,?,?,NULL,'observed',?,?)
                    ON CONFLICT(scope,id) DO UPDATE SET last_seen=excluded.last_seen''',
                    (self.scope,a.id,a.node,a.label,a.size,stamp,stamp))
                self.db.execute('INSERT OR REPLACE INTO latest VALUES(?,?,?,?)',(self.scope,a.node,a.label,a.id))
                if change != 'unchanged':
                    title=titles.get(a.node,a.node)
                    self.db.execute('INSERT INTO events(date,scope,node,title,label,change,old_id,new_id,detail) VALUES(?,?,?,?,?,?,?,?,?)',
                        (stamp,self.scope,a.node,title,a.label,change,old,a.id,'Source identity; not a comparison of video scenes'))
                    changes.append((title,change))
        return changes

    def completed(self, row):
        with self.db:
            self.db.execute('UPDATE versions SET size=?,sha256=?,status=?,last_seen=? WHERE scope=? AND id=?',
                (row['size'],row['sha256'],row['status'],now(),self.scope,row['id']))
            if row['status']=='done' and row['sha256']:
                event=self.db.execute('SELECT * FROM events WHERE scope=? AND new_id=? ORDER BY id DESC LIMIT 1',
                                      (self.scope,row['id'])).fetchone()
                if event and event['old_id']:
                    old=self.db.execute('SELECT sha256 FROM versions WHERE scope=? AND id=?',
                                        (self.scope,event['old_id'])).fetchone()
                    detail=('identical_bytes' if old[0]==row['sha256'] else 'different_bytes') if old and old[0] else 'previous_hash_unavailable'
                    self.db.execute('UPDATE events SET detail=? WHERE id=?',(detail,event['id']))

    def close(self):
        try:
            rows=[dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id')]
            temporary=self.path.with_name('events.jsonl.tmp')
            temporary.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(self.path.with_name('events.jsonl'))
        finally:
            self.db.close()
