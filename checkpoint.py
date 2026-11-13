import hashlib
import json
import sqlite3

class Checkpoints:
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, step INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)')
        self.db.commit()

    def save(self, name, step, engine):
        payload = json.dumps(engine.export(), sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)',
                            (name, step, payload, digest))
        return {'name': name, 'step': step, 'digest': digest}

    def read(self, name):
        step, payload, digest = self.db.execute(
            'SELECT step,payload,digest FROM checkpoints WHERE name=?', (name,)).fetchone()
        assert hashlib.sha256(payload.encode()).hexdigest() == digest, 'CHECKPOINT_CORRUPT'
        return step, json.loads(payload)

    def close(self):
        self.db.close()
