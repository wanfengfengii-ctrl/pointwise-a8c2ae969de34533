SIDES = ('L', 'R')
PARTS = ('0', '1')

class Progress:
    def __init__(self):
        self.floor = {s + p: 0 for s in SIDES for p in PARTS}
        self.idle = {k: False for k in self.floor}
        self.sent = {s: 0 for s in SIDES}

    def update(self, op):
        part = op['side'] + op['part']
        if op['op'] == 'advance':
            assert op['floor'] >= self.floor[part], 'NON_MONOTONE_PROGRESS'
            self.floor[part] = op['floor']
        elif op['op'] == 'idle':
            self.idle[part] = True
        elif op['op'] == 'resume':
            self.idle[part] = False
        for side in SIDES:
            active = [self.floor[side + p] for p in PARTS
                      if not self.idle[side + p]]
            if active:
                self.sent[side] = max(self.sent[side], min(active))

    def accepts(self, row):
        part = row['side'] + row['part']
        return not self.idle[part] and row['t'] >= self.floor[part]

    def export(self):
        return {'floor': {k: v for k, v in self.floor.items() if not self.idle[k]},
                'idle': dict(self.idle), 'sent': dict(self.sent)}

    @classmethod
    def load(cls, state):
        result = cls()
        result.sent = dict(state['sent'])
        result.idle = dict(state['idle'])
        result.floor = {k: state['floor'].get(k, result.sent[k[0]])
                        for k in result.floor}
        return result
