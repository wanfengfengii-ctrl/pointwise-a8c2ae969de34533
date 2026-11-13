import argparse
import json
from pathlib import Path
from checkpoint import Checkpoints
from joiner import Joiner

def read_bundle(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def replay(bundle, restore_at=None, db_path=':memory:'):
    engine = Joiner(bundle['width'])
    checkpoints = Checkpoints(db_path)
    history = []
    try:
        for step, op in enumerate(bundle['ops'], 1):
            if op['op'] == 'checkpoint':
                receipt = checkpoints.save(op['name'], step, engine)
                history.append({'step': step, 'kind': 'checkpoint',
                                'receipt': receipt, 'state': engine.export()})
                if op['name'] == restore_at:
                    saved_step, state = checkpoints.read(op['name'])
                    assert saved_step == step
                    engine = Joiner.load(state)
                    history.append({'step': step, 'kind': 'restore',
                                    'state': engine.export()})
            else:
                engine.apply(op, step)
                history.extend(engine.trace)
                engine.trace.clear()
            history.append({'step': step, 'kind': 'view', **engine.result()})
        return {**engine.result(), 'trace': history}
    finally:
        checkpoints.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle')
    parser.add_argument('--restore-at')
    parser.add_argument('--db', default=':memory:')
    args = parser.parse_args()
    print(json.dumps(replay(read_bundle(args.bundle), args.restore_at, args.db),
                     ensure_ascii=False, sort_keys=True))

if __name__ == '__main__':
    main()
