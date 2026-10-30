import os
import fcntl
import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        if (self.directory / 'ytsd-dev.sqlite3').exists():
            raise RuntimeError('ReelDock requires its own empty data directory. Do not use YTSD app data.')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'reeldock.sqlite3'
        self._local = threading.local()
        with self.connect() as db:
            # Persist WAL once at startup, including upgrades of existing stores.
            # Readers can then serve the Guide while the worker commits writes.
            mode = db.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode.lower() != 'wal':
                raise RuntimeError('App-data storage must support SQLite WAL mode.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dismissed_alerts (id TEXT PRIMARY KEY, dismissed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS activity_tasks (
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,state TEXT NOT NULL,
                    message TEXT NOT NULL,progress REAL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS activity_previews (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL);

                CREATE TABLE IF NOT EXISTS activity_events (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, category TEXT NOT NULL,
                    title TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS activity_event_date ON activity_events(created DESC);
                CREATE TABLE IF NOT EXISTS activity_batches (
                    key TEXT PRIMARY KEY, event_id INTEGER NOT NULL, count INTEGER NOT NULL, updated REAL NOT NULL);
                DROP TRIGGER IF EXISTS activity_task_finished_insert;
                DROP TRIGGER IF EXISTS activity_task_finished_update;
                DROP TRIGGER IF EXISTS activity_task_finished_insert_v2;
                DROP TRIGGER IF EXISTS activity_task_finished_update_v2;
                CREATE TRIGGER IF NOT EXISTS activity_task_finished_insert_v3 AFTER INSERT ON activity_tasks
                WHEN NEW.state IN ('complete','error','failed','cancelled')
                    AND NOT (NEW.id IN ('youtube-artwork','m3u-exports','m3u-catalogue') AND NEW.state='complete')
                BEGIN INSERT INTO activity_events(created,category,title,state,message)
                    VALUES(NEW.updated,'Background work',NEW.title,NEW.state,NEW.message); END;
                CREATE TRIGGER IF NOT EXISTS activity_task_finished_update_v3 AFTER UPDATE ON activity_tasks
                WHEN NEW.state IN ('complete','error','failed','cancelled')
                    AND NOT (NEW.id IN ('youtube-artwork','m3u-exports','m3u-catalogue') AND NEW.state='complete')
                    AND (OLD.state != NEW.state OR OLD.message != NEW.message)
                BEGIN INSERT INTO activity_events(created,category,title,state,message)
                    VALUES(NEW.updated,'Background work',NEW.title,NEW.state,NEW.message); END;
                CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY CHECK(id=1),
                    username TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (bucket TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                    locked_until REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)
        from .event_log import merge_old_artwork, merge_old_metadata
        merge_old_artwork(self)
        merge_old_metadata(self)

    def reset_pending(self):
        try:
            return self.get('reset_in_progress') == '1'
        except (sqlite3.Error, OSError):
            logging.warning('Could not read reset status; background work will retry shortly')
            return True

    def begin_operation(self):
        local = self._local
        depth = getattr(local, 'operation_depth', 0)
        if not depth:
            lock = (self.directory / 'operation.lock').open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_SH)
            except BaseException:
                lock.close()
                raise
            local.operation_lock = lock
        local.operation_depth = depth + 1

    def end_operation(self):
        local = self._local
        depth = getattr(local, 'operation_depth', 0)
        if depth:
            local.operation_depth = depth - 1
            if depth == 1:
                local.operation_lock.close()
                local.operation_lock = None

    @contextmanager
    def operation(self):
        self.begin_operation()
        try:
            yield
        finally:
            self.end_operation()

    @contextmanager
    def background_operation(self):
        # Cancellation crosses broad provider-error handlers, but still runs
        # finally blocks that close sockets and terminate download processes.
        from .reset import ResetRequested
        with self.operation():
            previous = getattr(self._local, 'cancel_for_reset', False)
            self._local.cancel_for_reset = True
            try:
                yield
            except ResetRequested:
                pass
            finally:
                self._local.cancel_for_reset = previous

    @contextmanager
    def exclusive_operation(self):
        from .reset import ResetBusy
        with self.operation():
            lock = self._local.operation_lock
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # flock upgrades can release the shared lock; reacquire it.
                fcntl.flock(lock, fcntl.LOCK_SH)
                raise ResetBusy('ReelDock is busy. Cancel or finish active downloads and scans, then retry.') from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_SH)

    def _open(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def connect(self):
        """Yield a connection inside a transaction (committed on success).

        Inside a reuse scope (one web request) the thread
        keeps a single idle connection and hands it out again instead of
        opening and closing SQLite for every query. Nested calls still get
        their own connection, so transactions never merge. Outside a scope
        every call opens and closes its own connection, as before.
        """
        with self.operation():
            local = self._local
            db = getattr(local, 'idle', None) if getattr(local, 'scoped', False) else None
            if db is not None:
                local.idle = None
            else:
                db = self._open()
            try:
                with db:
                    if getattr(local, 'cancel_for_reset', False):
                        pending = db.execute("SELECT value FROM settings WHERE key='reset_in_progress'").fetchone()
                        if pending and pending[0] == '1':
                            from .reset import ResetRequested
                            raise ResetRequested()
                    yield db
            finally:
                if db.in_transaction:
                    db.rollback()
                if getattr(local, 'scoped', False) and getattr(local, 'idle', None) is None:
                    local.idle = db
                else:
                    db.close()

    def begin_reuse(self):
        self._local.scoped = True

    def end_reuse(self):
        local = self._local
        local.scoped = False
        db, local.idle = getattr(local, 'idle', None), None
        if db is not None:
            db.close()

    @contextmanager
    def reuse(self):
        """Share one connection for a block of work on this thread."""
        if getattr(self._local, 'scoped', False):
            yield
            return
        self.begin_reuse()
        try:
            yield
        finally:
            self.end_reuse()

    def get(self, key, default=''):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, str(value)))

    def user(self):
        with self.connect() as db:
            return db.execute('SELECT * FROM users WHERE id=1').fetchone()

    def secret_file(self, name):
        path = self.directory / name
        if not path.exists():
            try:
                with path.open('x', encoding='utf-8') as file:
                    os.chmod(path, 0o600)
                    file.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        return path.read_text(encoding='utf-8').strip()
