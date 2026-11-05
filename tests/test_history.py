import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from telegram_archive.miniapps import Catalogue,Node
from telegram_archive.miniapps.history import History

class HistoryTests(unittest.TestCase):
 def test_survives_media_deletion_and_distinguishes_changes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);path=root/'profile/history.sqlite3';media=root/'downloads';media.mkdir()
   node=Node('lesson:1','','lesson','Original')
   asset=NS(id='old',node=node.id,label='video:0',size=10)
   h=History(path,'edify',1,2)
   self.assertEqual(h.observe(Catalogue([node],[asset])),[('Original','new_lesson')])
   h.completed(dict(id='old',size=10,sha256='aaa',status='done'));h.close()
   shutil.rmtree(media)
   h=History(path,'edify',1,2)
   self.assertEqual(h.observe(Catalogue([node],[asset])),[])
   node.title='Renamed';asset.id='new'
   self.assertEqual(h.observe(Catalogue([node],[asset])),[('Renamed','source_changed')])
   h.completed(dict(id='new',size=10,sha256='aaa',status='done'))
   self.assertEqual(h.db.execute('SELECT detail FROM events ORDER BY id DESC LIMIT 1').fetchone()[0],'identical_bytes')
   h.close()
   self.assertTrue(path.with_name('events.jsonl').is_file())
 def test_accounts_are_isolated(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'history.sqlite3';node=Node('l','','lesson','Lesson');a=NS(id='a',node='l',label='video:0',size=1)
   for account in (1,2):
    h=History(path,'edify',account,3)
    self.assertEqual(h.observe(Catalogue([node],[a]))[0][1],'new_lesson');h.close()
 def test_new_attachment_in_existing_lesson(self):
  with tempfile.TemporaryDirectory() as tmp:
   h=History(Path(tmp)/'history.sqlite3','edify',1,2);n=Node('l','','lesson','Lesson')
   h.observe(Catalogue([n],[]))
   a=NS(id='pdf',node='l',label='material:1',size=2)
   self.assertEqual(h.observe(Catalogue([n],[a]))[0][1],'new_attachment');h.close()
