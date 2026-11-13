#!/usr/bin/env python3
"""双路采样关联事故包的诊断分析入口(只诊断,不改生产源码)。

用法:
  python3 analysis.py run BUNDLE     重跑原入口与派生对照,独立重算期望与区间,写 evidence.json
  python3 analysis.py verify BUNDLE  不用旧缓存重算,核对 evidence.json 与 REPORT.md 的数值和见证
  python3 analysis.py demo BUNDLE    展示真实明细差异与至少一个区间见证

只接收本事故包(data/incident.json)或保持检查点名称的上述派生包。
三者均向 stdout 输出 JSON 对象;verify 一致时退出 0,不一致时非 0。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVIDENCE_PATH = ROOT / 'evidence.json'
REPORT_PATH = ROOT / 'REPORT.md'
DERIVED_DIR = ROOT / 'derived'
MARKER = 'JOIN_IDLE_STATE_LOSS'
SIDES = ('L', 'R')
PARTS = ('0', '1')

# ---------------------------------------------------------------- 基础工具

def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def run_entry(argv):
    """以子进程重放公开入口,留存原始 stdout/exit。"""
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    return {'argv': argv, 'exit': proc.returncode,
            'stdout': proc.stdout, 'stderr': proc.stderr}

def resolve_bundle(arg):
    path = Path(arg)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()

# ------------------------------------------------- 独立业务计算(不依赖生产代码)

def expected_pairs(bundle):
    """按 CONTRACT 业务语义独立计算最终明细:全部录制事件决定,
    同 key 多对多,L.t <= R.t <= L.t+width,输出 [L.id,R.id,L.v-R.v]。"""
    events = [op for op in bundle['ops'] if op['op'] == 'event']
    out = []
    for left in events:
        if left['side'] != 'L':
            continue
        for right in events:
            if (right['side'] == 'R' and left['key'] == right['key']
                    and left['t'] <= right['t'] <= left['t'] + bundle['width']):
                out.append([left['id'], right['id'], left['v'] - right['v']])
    return sorted(out)

def committed_floors(bundle, upto_step):
    """原始安全下界:仅由 advance 承诺构成(初值 0,同分区单调)。
    idle/resume 按 CONTRACT 不产生时间下界,sent 只是关联器内部视图。"""
    floors = {s + p: 0 for s in SIDES for p in PARTS}
    for step, op in enumerate(bundle['ops'], 1):
        if step > upto_step:
            break
        if op['op'] == 'advance':
            floors[op['side'] + op['part']] = op['floor']
    return floors

def match_window(side, t, width):
    """已见记录(侧 side,时间 t)的对侧匹配时间窗。"""
    return (t, t + width) if side == 'L' else (t - width, t)

def find_witnesses(bundle, side, part, key, interval, after_step):
    """在 after_step 之后、来自指定分区、同 key、t 落在闭区间内的真实记录。"""
    lo, hi = interval
    out = []
    for step, op in enumerate(bundle['ops'], 1):
        if (op['op'] == 'event' and step > after_step
                and op['side'] == side and op['part'] == part
                and op['key'] == key and lo <= op['t'] <= hi):
            out.append({'id': op['id'], 't': op['t'], 'step': step})
    return out

# ------------------------------------------- 生产关联器的在途观测(只读,不改行为)

def instrumented(bundle, restore_at=None):
    """用生产 Joiner/Checkpoints 逐步执行,记录每步内部状态。
    恢复后的 floor 在 export 中不可见(idle 分区被丢弃),只能在途观测。"""
    sys.path.insert(0, str(ROOT))
    from joiner import Joiner
    from checkpoint import Checkpoints
    engine = Joiner(bundle['width'])
    cps = Checkpoints()
    snaps = {}
    restore_step = None
    try:
        for step, op in enumerate(bundle['ops'], 1):
            tag = op['op']
            if op['op'] == 'checkpoint':
                cps.save(op['name'], step, engine)
                if op['name'] == restore_at:
                    _, state = cps.read(op['name'])
                    engine = Joiner.load(state)
                    restore_step = step
                    tag = 'restore'
            else:
                engine.apply(op, step)
            snaps[step] = {
                'tag': tag,
                'floor': dict(engine.progress.floor),
                'sent': dict(engine.progress.sent),
                'idle': dict(engine.progress.idle),
                'rows': {s: sorted(engine.rows[s]) for s in SIDES},
                'matches': sorted([list(k) for k in engine.matches]),
            }
    finally:
        cps.close()
    return {'restore_step': restore_step, 'snaps': snaps}

# ------------------------------------------------------------ 区间影响分析

def eviction_analysis(bundle, replay_output):
    """对轨迹中的每次候选淘汰:假设下一条新 id、同 key 的对侧记录来自
    指定分区(允许先 resume,不再 advance),用原始安全下界给出仍合法、
    本可匹配、当前实现却不再能输出的整数时间闭区间。空区间不输出。"""
    width = bundle['width']
    events = {op['id']: op for op in bundle['ops'] if op['op'] == 'event'}
    results = []
    for item in replay_output['trace']:
        if item.get('kind') != 'evict':
            continue
        step, rid, side = item['step'], item['id'], item['side']
        ev = events[rid]
        other = 'R' if side == 'L' else 'L'
        win = match_window(side, ev['t'], width)
        floors = committed_floors(bundle, step)
        intervals = {}
        for part in PARTS:
            lo = max(0, floors[other + part], win[0])
            hi = win[1]
            if lo <= hi:  # 闭区间非空才输出,边界相邻已合并为单一区间
                intervals[other + part] = {
                    'interval': [lo, hi],
                    'realized_by': find_witnesses(bundle, other, part,
                                                  ev['key'], [lo, hi], step),
                }
        results.append({'step': step, 'id': rid, 'side': side,
                        'key': ev['key'], 't': ev['t'], 'cut': item['cut'],
                        'match_window': list(win),
                        'mechanism': 'candidate_evicted',
                        'intervals': intervals})
    return results

def resume_analysis(bundle, snaps, restore_step):
    """恢复后首次 resume 的各分区:按此前已见的对侧记录逐条核定。
    仍留存者失去的区间 = 匹配窗 ∩ [原始下界, 恢复后floor);已被淘汰者
    失去的区间 = 匹配窗 ∩ [原始下界, +∞)。空区间不输出。"""
    width = bundle['width']
    resume_step = None
    resume_op = None
    for step, op in enumerate(bundle['ops'], 1):
        if op['op'] == 'resume' and (restore_step is None or step > restore_step):
            resume_step, resume_op = step, op
            break
    if resume_step is None:
        return None
    part = resume_op['side'] + resume_op['part']
    other = 'R' if resume_op['side'] == 'L' else 'L'
    snap = snaps[resume_step]
    restored_floor = snap['floor'][part]
    original_floor = committed_floors(bundle, resume_step)[part]
    retained = set(snap['rows'][other])
    records = []
    for step, op in enumerate(bundle['ops'], 1):
        if op['op'] != 'event' or op['side'] != other or step >= resume_step:
            continue
        win = match_window(other, op['t'], width)
        lo = max(0, original_floor, win[0])
        hi = win[1]
        if lo > hi:
            continue
        if op['id'] in retained:
            # 记录仍在,但恢复后的 floor 会拒收 t < restored_floor 的合法记录
            hi2 = min(hi, restored_floor - 1)
            if restored_floor <= lo or hi2 < lo:
                continue
            interval, mechanism = [lo, hi2], 'admission_rejected'
        else:
            interval, mechanism = [lo, hi], 'candidate_evicted'
        records.append({
            'id': op['id'], 'key': op['key'], 't': op['t'],
            'retained': op['id'] in retained,
            'interval': interval, 'mechanism': mechanism,
            'realized_by': find_witnesses(bundle, resume_op['side'],
                                          resume_op['part'], op['key'],
                                          interval, resume_step),
        })
    return {'resume_step': resume_step, 'partition': part,
            'restore_step': restore_step,
            'original_floor': original_floor,
            'restored_floor': restored_floor,
            'records': records}

# ------------------------------------------------------------ 派生对照包

def build_derived(bundle):
    """仅移动通知位置构造派生包,不改事件内容、相对顺序与代码:
    1) idle_after_advance   把 idle 推迟到该次进度通知之后;
    2) advance_after_resume 把该次进度通知推迟到分区恢复之后。"""
    ops = bundle['ops']
    i_idle = next((i for i, o in enumerate(ops) if o['op'] == 'idle'), None)
    if i_idle is None:
        return {}
    tgt = ops[i_idle]
    i_res = next((i for i, o in enumerate(ops)
                  if o['op'] == 'resume' and o['side'] == tgt['side']
                  and o['part'] == tgt['part'] and i > i_idle), None)
    i_adv = next((i for i, o in enumerate(ops)
                  if o['op'] == 'advance' and o['side'] == tgt['side']
                  and o['part'] != tgt['part'] and i_idle < i < (i_res or 0)), None)
    if i_res is None or i_adv is None:
        return {}
    ops1 = [o for i, o in enumerate(ops) if i != i_idle]
    ops1.insert(i_adv, ops[i_idle])
    ops2 = [o for i, o in enumerate(ops) if i != i_adv]
    j_res = next(i for i, o in enumerate(ops2)
                 if o['op'] == 'resume' and o['side'] == tgt['side']
                 and o['part'] == tgt['part'])
    ops2.insert(j_res + 1, ops[i_adv])
    return {'idle_after_advance': {'width': bundle['width'], 'ops': ops1},
            'advance_after_resume': {'width': bundle['width'], 'ops': ops2}}

def validate_bundle(bundle):
    """派生包必须遵守录制承诺:advance 单调;idle..resume 之间无该分区事件;
    事件 t 不低于该分区已承诺下界。"""
    floors = {s + p: 0 for s in SIDES for p in PARTS}
    idle = set()
    for op in bundle['ops']:
        key = op.get('side', '') + op.get('part', '')
        if op['op'] == 'advance':
            assert op['floor'] >= floors[key], 'NON_MONOTONE_PROGRESS'
            floors[key] = op['floor']
        elif op['op'] == 'idle':
            idle.add(key)
        elif op['op'] == 'resume':
            idle.discard(key)
        elif op['op'] == 'event':
            assert key not in idle and op['t'] >= floors[key], 'BUNDLE_PROMISE_BROKEN'

# ------------------------------------------------------------ 证据计算

def replay_modes(bundle_path, checkpoints):
    """经公开入口 replay.py 重放不中断与各检查点恢复。"""
    out = {}
    for mode in ['live'] + list(checkpoints):
        argv = ['python3', 'replay.py', str(bundle_path)]
        if mode != 'live':
            argv += ['--restore-at', mode]
        entry = run_entry(argv)
        out[mode] = {'entry': entry, 'output': json.loads(entry['stdout'])}
    return out

def mode_summary(bundle, expected, replay_output):
    pairs = replay_output['pairs']
    return {
        'pairs': pairs,
        'total': replay_output['total'],
        'missing_pairs': [p for p in expected if p not in pairs],
        'extra_pairs': [p for p in pairs if p not in expected],
        'rejected': [t['id'] for t in replay_output['trace']
                     if t['kind'] == 'admission' and not t['accepted']],
        'evicted': [{'step': t['step'], 'id': t['id'], 'cut': t['cut']}
                    for t in replay_output['trace'] if t['kind'] == 'evict'],
    }

def checkpoint_conversion(bundle, replays, instrs, checkpoints):
    """逐检查点对比:写入前已承诺 floor、导出载荷中的 floor、恢复后 floor。"""
    table = {}
    for name in checkpoints:
        step = next(i for i, o in enumerate(bundle['ops'], 1)
                    if o['op'] == 'checkpoint' and o['name'] == name)
        committed = instrs['live']['snaps'][step]['floor']
        exported = next(t['state']['progress']['floor']
                        for t in replays['live']['output']['trace']
                        if t['kind'] == 'checkpoint' and t['receipt']['name'] == name)
        restored = instrs[name]['snaps'][step]['floor']
        changed = {k: {'committed': committed[k], 'restored': restored[k]}
                   for k in committed if committed[k] != restored[k]}
        table[name] = {'step': step, 'committed': committed,
                       'exported': exported, 'restored': restored,
                       'changed_by_restore': changed}
    return table

def zero_sum_groups(bundle, expected, missing):
    """按 key 聚合漏配对,展示零和掩盖:漏配组的差值和为 0。"""
    keys = {}
    for op in bundle['ops']:
        if op['op'] == 'event':
            keys[op['id']] = op['key']
    groups = {}
    for pair in missing:
        key = keys[pair[0]]
        groups.setdefault(key, {'pairs': [], 'sum': 0})
        groups[key]['pairs'].append(pair)
        groups[key]['sum'] += pair[2]
    return groups

def parse_incident_log(text):
    runs = []
    for line in text.splitlines():
        if line.startswith('run='):
            runs.append({'header': line.strip(),
                         'exit1': 'exit=1' in line})
    return {'runs': len(runs),
            'all_exit_1': all(r['exit1'] for r in runs),
            'marker_lines': text.count(MARKER),
            'text': text}

def compute_evidence(bundle_arg):
    bundle_path = resolve_bundle(bundle_arg)
    bundle = load_json(bundle_path)
    validate_bundle(bundle)
    checkpoints = [op['name'] for op in bundle['ops'] if op['op'] == 'checkpoint']
    modes = ['live'] + checkpoints

    replays = replay_modes(bundle_path, checkpoints)
    instrs = {m: instrumented(bundle, None if m == 'live' else m) for m in modes}
    expected = expected_pairs(bundle)

    per_mode = {}
    eviction_intervals = {}
    resume = {}
    for m in modes:
        per_mode[m] = mode_summary(bundle, expected, replays[m]['output'])
        eviction_intervals[m] = eviction_analysis(bundle, replays[m]['output'])
        resume[m] = resume_analysis(bundle, instrs[m]['snaps'],
                                    instrs[m]['restore_step'])

    first_loss = None
    for item in replays['live']['output']['trace']:
        if item.get('kind') == 'evict':
            step = item['step']
            ids = [t['id'] for t in replays['live']['output']['trace']
                   if t.get('kind') == 'evict' and t['step'] == step]
            first_loss = {'step': step, 'kind': 'evict', 'ids': ids,
                          'cut': item['cut']}
            break

    conversion = checkpoint_conversion(bundle, replays, instrs, checkpoints)

    # 派生对照包:仅移动通知位置
    derived = {}
    DERIVED_DIR.mkdir(exist_ok=True)
    for name, content in build_derived(bundle).items():
        validate_bundle(content)
        path = DERIVED_DIR / f'{name}.json'
        path.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
        d_replays = replay_modes(path, checkpoints)
        d_first_loss_step = next(
            (t['step'] for t in d_replays['live']['output']['trace']
             if t.get('kind') == 'evict'), None)
        derived[name] = {
            'path': str(path.relative_to(ROOT)),
            'content': content,
            'modes': {m: mode_summary(content, expected, d_replays[m]['output'])
                      for m in modes},
            'first_evict_step': d_first_loss_step,
        }

    # 原入口证据:正常回归 + 连续两次同标记失败
    checks_normal = run_entry(['python3', 'checks.py', 'normal'])
    incident_runs = [run_entry(['python3', 'checks.py', 'incident'])
                     for _ in range(2)]
    log_path = bundle_path.with_suffix('.log')
    recorded = parse_incident_log(log_path.read_text(encoding='utf-8')) \
        if log_path.exists() else None

    return {
        'schema': 1,
        'bundle': {'path': str(bundle_path), 'width': bundle['width'],
                   'ops': len(bundle['ops']), 'checkpoints': checkpoints},
        'source_refs': source_refs(),
        'original_entries': {
            'checks_normal': checks_normal,
            'checks_incident_runs': incident_runs,
            'incident_log_recorded': recorded,
            'replay_modes': {m: replays[m]['entry'] for m in modes},
        },
        'expected': {'pairs': expected,
                     'total': sum(p[2] for p in expected)},
        'per_mode': per_mode,
        'first_irreversible_loss': first_loss,
        'checkpoint_conversion': conversion,
        'eviction_intervals': eviction_intervals,
        'resume_analysis': resume,
        'derived_bundles': derived,
        'zero_sum_masking': {
            'expected_total': sum(p[2] for p in expected),
            'totals': {m: per_mode[m]['total'] for m in modes},
            'lost_groups': {m: zero_sum_groups(bundle, expected,
                                               per_mode[m]['missing_pairs'])
                            for m in modes},
        },
        'controls': controls(bundle, expected, per_mode, conversion,
                             checks_normal),
    }

def source_refs():
    """源码引用:行号 + 行文本,随证据保存。"""
    refs = {
        'progress.py': {
            '13-14': 'advance 提交分区下界(单调断言)',
            '15-18': 'idle/resume 仅翻转标志,不产生时间下界',
            '19-23': 'sent[side] 只对活跃分区取 min 的缺陷位置',
            '25-27': '接纳判定:非 idle 且 t>=floor',
            '29-31': 'export 丢弃 idle 分区的 floor',
            '33-40': 'load 用 sent[side] 回填缺失 floor',
        },
        'joiner.py': {
            '18-22': '事件接纳闸口,拒收直接返回',
            '25-32': '到达时对现存候选做匹配',
            '41-50': '按 sent[other] 淘汰候选(不可逆丢失点)',
        },
        'replay.py': {'15-25': '检查点写入与 --restore-at 恢复流程'},
        'checkpoint.py': {'19-23': '摘要仅校验载荷完整,不校验语义'},
        'checks.py': {'5-15': '独立期望明细计算', '28-43': 'incident 三模式核对'},
    }
    out = {}
    for fname, ranges in refs.items():
        lines = (ROOT / fname).read_text(encoding='utf-8').splitlines()
        out[fname] = {}
        for rng, note in ranges.items():
            a, b = (int(x) for x in rng.split('-'))
            out[fname][rng] = {'note': note,
                               'text': [lines[i] for i in range(a - 1, b)]}
    return out

def controls(bundle, expected, per_mode, conversion, checks_normal):
    keys = {op['id']: op['key'] for op in bundle['ops'] if op['op'] == 'event'}
    unrelated = [p for p in expected
                 if all(p in per_mode[m]['pairs'] for m in per_mode)
                 and keys[p[0]] not in
                 {keys[q[0]] for m in per_mode for q in per_mode[m]['missing_pairs']}]
    zero_diff = [p for p in expected if p[2] == 0]
    return {
        'unrelated_key_pairs_present_all_modes': unrelated,
        'zero_diff_pairs': {
            'missing_in_all_modes': [p for p in zero_diff
                                     if all(p in per_mode[m]['missing_pairs']
                                            for m in per_mode)],
            'present_in_all_modes': [p for p in zero_diff
                                     if all(p in per_mode[m]['pairs']
                                            for m in per_mode)],
        },
        'safe_restore_normal_regression_ok':
            checks_normal['exit'] == 0
            and 'NORMAL_REGRESSION_OK' in checks_normal['stdout'],
        'restore_state_lossless': {name: not conv['changed_by_restore']
                                   for name, conv in conversion.items()},
    }

# ------------------------------------------------------------ 报告事实块

def compute_report_facts(ev):
    """REPORT.md 中机器可核对的事实子集(数值与见证)。"""
    modes = ['live'] + ev['bundle']['checkpoints']
    facts = {
        'expected_pairs': ev['expected']['pairs'],
        'expected_total': ev['expected']['total'],
        'modes': {m: {'pairs': ev['per_mode'][m]['pairs'],
                      'total': ev['per_mode'][m]['total'],
                      'rejected': ev['per_mode'][m]['rejected']}
                  for m in modes},
        'first_irreversible_loss_step': ev['first_irreversible_loss']['step'],
        'eviction_intervals': [
            {'step': e['step'], 'record': e['id'], 'partition': part,
             'interval': iv['interval'], 'realized_by': iv['realized_by']}
            for e in ev['eviction_intervals']['live']
            for part, iv in sorted(e['intervals'].items())],
        'resume_intervals_idle_mode': [
            {'record': r['id'], 'interval': r['interval'],
             'mechanism': r['mechanism'], 'realized_by': r['realized_by']}
            for r in ev['resume_analysis']['idle']['records']
        ] if ev['resume_analysis'].get('idle') else [],
        'checkpoint_floor_changes': {
            name: conv['changed_by_restore']
            for name, conv in ev['checkpoint_conversion'].items()},
        'derived': {name: {'first_evict_step': d['first_evict_step'],
                           'pairs_per_mode':
                           {m: len(d['modes'][m]['pairs'])
                            for m in d['modes']}}
                    for name, d in ev['derived_bundles'].items()},
        'zero_sum_lost_groups': ev['zero_sum_masking']['lost_groups'],
    }
    return facts

# ------------------------------------------------------------ 子命令

def cmd_run(bundle_arg):
    ev = compute_evidence(bundle_arg)
    EVIDENCE_PATH.write_text(json.dumps(ev, ensure_ascii=False,
                                        sort_keys=True, indent=1),
                             encoding='utf-8')
    out = {'ok': True, 'evidence': str(EVIDENCE_PATH),
           'expected_pairs': len(ev['expected']['pairs']),
           'pairs_per_mode': {m: len(ev['per_mode'][m]['pairs'])
                              for m in ev['per_mode']},
           'first_irreversible_loss': ev['first_irreversible_loss'],
           'report_facts': compute_report_facts(ev)}
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0

def diff_paths(a, b, path='', out=None, limit=20):
    out = out if out is not None else []
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append(f'{path}: type {type(a).__name__} != {type(b).__name__}')
        return out
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f'{path}.{k}: missing in recomputed')
            elif k not in b:
                out.append(f'{path}.{k}: missing in evidence.json')
            else:
                diff_paths(a[k], b[k], f'{path}.{k}', out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f'{path}: len {len(a)} != {len(b)}')
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diff_paths(x, y, f'{path}[{i}]', out, limit)
    elif a != b:
        out.append(f'{path}: {a!r} != {b!r}')
    return out

def cmd_verify(bundle_arg):
    checks = []
    ok = True
    fresh = compute_evidence(bundle_arg)  # 不用旧计算缓存,全部重算

    if EVIDENCE_PATH.exists():
        stored = load_json(EVIDENCE_PATH)
        diffs = diff_paths(fresh, stored)
        checks.append({'check': 'evidence.json 与重算一致',
                       'ok': not diffs, 'diffs': diffs})
        ok &= not diffs
    else:
        checks.append({'check': 'evidence.json 存在', 'ok': False})
        ok = False

    facts = compute_report_facts(fresh)
    if REPORT_PATH.exists():
        m = re.search(r'<!-- report-facts -->\s*```json\s*(.*?)```',
                      REPORT_PATH.read_text(encoding='utf-8'), re.S)
        reported = json.loads(m.group(1)) if m else None
        diffs = diff_paths(facts, reported) if reported is not None else \
            ['REPORT.md 缺少 report-facts 块']
        checks.append({'check': 'REPORT.md 数值和见证与重算一致',
                       'ok': not diffs, 'diffs': diffs})
        ok &= not diffs
    else:
        checks.append({'check': 'REPORT.md 存在', 'ok': False})
        ok = False

    for name, d in fresh['derived_bundles'].items():
        on_disk = load_json(ROOT / d['path'])
        same = on_disk == d['content']
        checks.append({'check': f'派生包 {name} 落盘内容与证据一致', 'ok': same})
        ok &= same

    normal_ok = fresh['controls']['safe_restore_normal_regression_ok']
    checks.append({'check': 'normal 回归断言通过', 'ok': normal_ok})
    ok &= normal_ok

    runs = fresh['original_entries']['checks_incident_runs']
    twice = (len(runs) == 2
             and all(r['exit'] != 0 and MARKER in r['stdout'] for r in runs))
    checks.append({'check': '原故障连续两次同标记失败', 'ok': twice})
    ok &= twice

    print(json.dumps({'ok': ok, 'checks': checks},
                     ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1

def cmd_demo(bundle_arg):
    ev = compute_evidence(bundle_arg)
    modes = ['live'] + ev['bundle']['checkpoints']
    witnesses = []
    for e in ev['eviction_intervals']['live']:
        for part, iv in sorted(e['intervals'].items()):
            for w in iv['realized_by']:
                pair = next(p for p in ev['expected']['pairs']
                            if {p[0], p[1]} == {e['id'], w['id']})
                witnesses.append({
                    'kind': 'candidate_evicted', 'record': e['id'],
                    'evicted_step': e['step'], 'partition': part,
                    'interval': iv['interval'], 'witness': w,
                    'lost_pair': pair,
                    'missing_in': [m for m in modes
                                   if pair in ev['per_mode'][m]['missing_pairs']]})
    for r in (ev['resume_analysis'].get('idle') or {}).get('records', []):
        for w in r['realized_by']:
            pair = next(p for p in ev['expected']['pairs']
                        if {p[0], p[1]} == {r['id'], w['id']})
            witnesses.append({
                'kind': r['mechanism'], 'record': r['id'],
                'resume_step': ev['resume_analysis']['idle']['resume_step'],
                'partition': ev['resume_analysis']['idle']['partition'],
                'restored_floor':
                    ev['resume_analysis']['idle']['restored_floor'],
                'interval': r['interval'], 'witness': w,
                'lost_pair': pair,
                'missing_in': [m for m in modes
                               if pair in ev['per_mode'][m]['missing_pairs']]})
    out = {
        'bundle': ev['bundle']['path'],
        'expected_pairs': ev['expected']['pairs'],
        'detail_differences': {
            m: {'pairs': ev['per_mode'][m]['pairs'],
                'total': ev['per_mode'][m]['total'],
                'missing_pairs': ev['per_mode'][m]['missing_pairs'],
                'rejected': ev['per_mode'][m]['rejected']}
            for m in modes},
        'interval_witnesses': witnesses,
        'zero_sum_masking': ev['zero_sum_masking'],
    }
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0

def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('run', 'verify', 'demo'):
        raise SystemExit('use: analysis.py run|verify|demo BUNDLE')
    cmd = sys.argv[1]
    if cmd == 'run':
        return cmd_run(sys.argv[2])
    if cmd == 'verify':
        return cmd_verify(sys.argv[2])
    return cmd_demo(sys.argv[2])

if __name__ == '__main__':
    sys.exit(main())
