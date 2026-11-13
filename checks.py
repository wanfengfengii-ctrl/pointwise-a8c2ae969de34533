import json
import sys
from replay import read_bundle, replay

def expected(bundle):
    rows = [op for op in bundle['ops'] if op['op'] == 'event']
    pairs = []
    for left in rows:
        if left['side'] != 'L':
            continue
        for right in rows:
            if (right['side'] == 'R' and left['key'] == right['key'] and
                    0 <= right['t'] - left['t'] <= bundle['width']):
                pairs.append([left['id'], right['id'], left['v'] - right['v']])
    return sorted(pairs)

def normal():
    bundle = read_bundle('data/normal.json')
    want = [['l0', 'r0', 6], ['l1', 'r1', -2], ['l2', 'r2', 0]]
    assert expected(bundle) == want
    for mode in (None, 'safe'):
        got = replay(bundle, mode)
        assert got['pairs'] == want, got
        assert got['total'] == 4
        assert all(x['accepted'] for x in got['trace'] if x['kind'] == 'admission')
    print('NORMAL_REGRESSION_OK')

def incident():
    bundle = read_bundle('data/incident.json')
    want = expected(bundle)
    bad = False
    for mode in (None, 'before', 'idle'):
        got = replay(bundle, mode)
        summary = {'mode': mode or 'live', 'expected_pairs': len(want),
                   'actual_pairs': len(got['pairs']), 'pairs': got['pairs'],
                   'total': got['total'],
                   'rejected': [x['id'] for x in got['trace']
                                if x['kind'] == 'admission' and not x['accepted']]}
        print(json.dumps(summary, sort_keys=True))
        bad |= got['pairs'] != want
    if bad:
        print('JOIN_IDLE_STATE_LOSS')
        raise SystemExit(1)

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'normal':
        normal()
    elif mode == 'incident':
        incident()
    elif mode == 'verify':
        normal()
        incident()
    else:
        raise SystemExit('use normal, incident or verify')
