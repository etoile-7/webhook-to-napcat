"""Persistent per-notification, per-target delivery receipts."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time


class DeliveryBusy(Exception):
    pass


class DeliveryConflict(Exception):
    pass


class DeliveryStore:
    def __init__(self, root: str):
        self.root = Path(root) / 'delivery'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / 'receipts.sqlite', timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, created REAL NOT NULL,
                receipt TEXT);
            CREATE TABLE IF NOT EXISTS plans (
                notification TEXT PRIMARY KEY, chunks TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resolutions (
                notification TEXT NOT NULL, step TEXT NOT NULL, resolved REAL NOT NULL,
                outcome TEXT NOT NULL, evidence TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS steps (
                notification TEXT NOT NULL, step TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY (notification, step));
        ''')

    @contextmanager
    def claim(self, payload):
        key = payload['notification_id']
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        lock_path = self.root / (hashlib.sha256(key.encode()).hexdigest() + '.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeliveryBusy from None
            try:
                row = self.db.execute('SELECT digest, receipt FROM notifications WHERE id=?', (key,)).fetchone()
                if row and row[0] != digest:
                    raise DeliveryConflict
                if not row:
                    with self.db:
                        self.db.execute('INSERT INTO notifications VALUES (?, ?, ?, NULL)',
                                        (key, digest, time.time()))
                # A previous owner disappeared after starting an external call.
                # Its outcome cannot be recovered from an HTTP retry alone.
                with self.db:
                    self.db.execute("UPDATE steps SET state='uncertain' WHERE notification=? AND state='sending'", (key,))
                yield json.loads(row[1]) if row and row[1] else None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def text_plan(self, notification, chunks):
        """Freeze segmentation; changing chunk configuration cannot change retries."""
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO plans VALUES (?, ?)',
                            (notification, json.dumps(chunks, ensure_ascii=False)))
            return json.loads(self.db.execute('SELECT chunks FROM plans WHERE notification=?',
                                             (notification,)).fetchone()[0])

    def state(self, notification, step):
        row = self.db.execute('SELECT state FROM steps WHERE notification=? AND step=?',
                              (notification, step)).fetchone()
        return row[0] if row else 'pending'

    def set_state(self, notification, step, state):
        with self.db:
            self.db.execute('INSERT INTO steps VALUES (?, ?, ?) ON CONFLICT(notification, step) DO UPDATE SET state=excluded.state',
                            (notification, step, state))

    def finish(self, notification, receipt):
        with self.db:
            self.db.execute('UPDATE notifications SET receipt=? WHERE id=?',
                            (json.dumps(receipt), notification))

    def steps(self, notification):
        return [dict(step=row[0], state=row[1]) for row in self.db.execute(
            'SELECT step, state FROM steps WHERE notification=? ORDER BY step', (notification,))]

    def resolve(self, notification, step, *, outcome, evidence):
        """An operator supplies a verified outcome; this method never sends."""
        if outcome not in {'delivered', 'not-delivered'} or not evidence.strip():
            raise ValueError('a verified outcome and evidence are required')
        if not step.startswith('text:'):
            raise ValueError('only uncertain text delivery can be resolved')
        lock_path = self.root / (hashlib.sha256(notification.encode()).hexdigest() + '.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeliveryBusy from None
            try:
                if self.state(notification, step) not in {'sending', 'uncertain'}:
                    raise ValueError('step is not awaiting verification')
                state = 'confirmed' if outcome == 'delivered' else 'failed'
                with self.db:
                    self.db.execute('UPDATE steps SET state=? WHERE notification=? AND step=?',
                                    (state, notification, step))
                    self.db.execute('INSERT INTO resolutions VALUES (?, ?, ?, ?, ?)',
                                    (notification, step, time.time(), outcome, evidence))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def close(self):
        self.db.close()
