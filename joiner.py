from progress import Progress

class Joiner:
    def __init__(self, width):
        self.width = width
        self.progress = Progress()
        self.rows = {'L': {}, 'R': {}}
        self.matches = {}
        self.trace = []

    def _match(self, left, right):
        return (left['key'] == right['key'] and
                left['t'] <= right['t'] <= left['t'] + self.width)

    def apply(self, op, step):
        kind = op['op']
        if kind == 'event':
            accepted = self.progress.accepts(op)
            self.trace.append({'step': step, 'kind': 'admission',
                               'id': op['id'], 'accepted': accepted})
            if not accepted:
                return
            side = op['side']
            other = 'R' if side == 'L' else 'L'
            for candidate in self.rows[other].values():
                left, right = (op, candidate) if side == 'L' else (candidate, op)
                if self._match(left, right):
                    pair = (left['id'], right['id'])
                    self.matches[pair] = left['v'] - right['v']
                    self.trace.append({'step': step, 'kind': 'match',
                                       'pair': list(pair)})
            self.rows[side][op['id']] = dict(op)
        else:
            self.progress.update(op)
            self.trace.append({'step': step, 'kind': 'progress',
                               'floor': dict(self.progress.floor),
                               'idle': dict(self.progress.idle),
                               'sent': dict(self.progress.sent)})
        self._collect(step)

    def _collect(self, step):
        for side in ('L', 'R'):
            other = 'R' if side == 'L' else 'L'
            cut = self.progress.sent[other]
            for rid, row in list(self.rows[side].items()):
                end = row['t'] + self.width if side == 'L' else row['t']
                if end < cut:
                    del self.rows[side][rid]
                    self.trace.append({'step': step, 'kind': 'evict',
                                       'id': rid, 'side': side, 'cut': cut})

    def export(self):
        return {'width': self.width, 'progress': self.progress.export(),
                'rows': {s: {k: dict(v) for k, v in rows.items()}
                         for s, rows in self.rows.items()},
                'matches': [[a, b, v] for (a, b), v in sorted(self.matches.items())]}

    @classmethod
    def load(cls, state):
        result = cls(state['width'])
        result.progress = Progress.load(state['progress'])
        result.rows = state['rows']
        result.matches = {(a, b): v for a, b, v in state['matches']}
        return result

    def result(self):
        return {'pairs': [[a, b, v] for (a, b), v in sorted(self.matches.items())],
                'total': sum(self.matches.values()),
                'retained': {s: sorted(self.rows[s]) for s in ('L', 'R')}}
