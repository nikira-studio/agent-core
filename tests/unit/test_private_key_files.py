"""Synthetic permission/restore regressions; never inspect production keys."""
import os,stat,sqlite3
from pathlib import Path
from cryptography.fernet import Fernet
from app.security import encryption
from app.services import backup_service

def mode(path):return stat.S_IMODE(Path(path).stat().st_mode)

def test_existing_key_and_keyring_are_hardened(clean_db):
 from app.config import settings
 encryption.get_primary_key()
 for p in [settings.credential_key_path,settings.data_dir/'credential.keyring']:os.chmod(p,0o644)
 encryption.reset_key_cache();encryption.get_primary_key()
 assert mode(settings.credential_key_path)==0o600
 assert mode(settings.data_dir/'credential.keyring')==0o600

def test_atomic_write_is_private_even_with_permissive_umask(tmp_path):
 p=tmp_path/'credential.key';mask=os.umask(0)
 try:
  encryption.write_private_key(p,b'synthetic-only')
  assert p.read_bytes()==b'synthetic-only' and mode(p)==0o600
  p.chmod(0o644);encryption.write_private_key(p,b'replacement-only')
  assert mode(p)==0o600
 finally:os.umask(mask)
 assert not list(tmp_path.glob('credential.key.tmp-*'))

def test_rotation_backup_keys_are_private(clean_db):
 from app.config import settings
 encryption.get_primary_key();encryption.rotate_key()
 for p in [settings.credential_key_path,Path(str(settings.credential_key_path)+'.rotated.bak'),settings.data_dir/'credential.keyring',settings.data_dir/'credential.keyring.bak']:assert mode(p)==0o600

def test_restore_replaced_keys_and_backups_are_private(clean_db):
 from app.config import settings
 key=encryption.get_primary_key()
 archive=backup_service.build_backup_zip(str(clean_db),str(settings.credential_key_path),'synthetic-admin','test')
 os.chmod(settings.credential_key_path,0o644)
 os.chmod(settings.data_dir/'credential.keyring',0o644)
 ok,msg,_=backup_service.restore_from_zip(archive,str(clean_db),str(settings.credential_key_path))
 assert ok,msg
 assert mode(settings.credential_key_path)==0o600
 assert mode(settings.data_dir/'credential.keyring')==0o600
 assert encryption.get_primary_key()==key
 for p in (settings.data_dir/'backups').glob('credential.key*'):assert mode(p)==0o600

def test_restore_consumer_grants_are_inert(tmp_path):
 p=tmp_path/'synthetic.sqlite';con=sqlite3.connect(p)
 con.execute('CREATE TABLE credential_consumer_grants(id TEXT, revoked_at TEXT)')
 con.executemany('INSERT INTO credential_consumer_grants VALUES (?,?)',[('active',None),('revoked','2020-01-01T00:00:00+00:00')]);con.commit();con.close()
 backup_service._revoke_consumer_grants(str(p))
 con=sqlite3.connect(p);rows=dict(con.execute('SELECT id,revoked_at FROM credential_consumer_grants'));con.close()
 assert rows['active'] and rows['revoked']=='2020-01-01T00:00:00+00:00'
