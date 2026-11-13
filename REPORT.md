# 双路采样关联漏配诊断报告

事故包:`data/incident.json`(width=10,24 个 op,检查点 `before`@step11、`idle`@step14)。
诊断入口:`analysis.py run|verify|demo data/incident.json`;证据:`evidence.json`;派生对照包:`derived/`。
本轮只诊断,未改动 `progress.py`、`joiner.py`、`checkpoint.py`、`replay.py`、`checks.py` 与 `data/`。

## 1. 结论

同一触发条件引发两个独立缺陷,三种执行合计漏配 5 对,且全部被零和掩盖(total 恒为 25,与期望一致):

- **缺陷A(候选淘汰,三执行共有)**:R0 空闲期间 R1 的 advance 把 `sent[R]` 推到 100,关联器按 `sent` 淘汰了 L 侧候选,而 R0 从未承诺过该下界。R0 恢复后合法送达的 rA(t=25)再也找不到已被淘汰的 lA1/lA2/lA3。
- **缺陷B(检查点转换 + 接纳拒收,仅 idle 恢复)**:检查点导出丢弃 idle 分区的 floor,恢复时用 `sent[R]=100` 回填 R0 的 floor(真实承诺只有 0),导致恢复后合法记录 rA(t=25)、rB(t=97)被接纳闸口拒收,额外丢失 B 键两对。

**首次不可逆丢失:step 13**(`advance R1→100` 落在 R0 空闲期内)。该步内 `sent[R]` 由 0 跳为 100(`progress.py:23` 的 `max` 单调不可逆),同一 step 的 `_collect` 删除 lU、lA1、lA2、lA3(`joiner.py:48`),候选状态不可恢复;漏配在 step 16(rA 到达)显形。

## 2. 机制与源码定位

### 缺陷A:idle 被当成"已承诺 +∞"参与安全时间

- `progress.py:19-23`:`sent[side] = max(sent, min(活跃分区 floor))`,**idle 分区被排除在 min 之外**。CONTRACT.md 第 9 行明确"idle 本身不承诺未来时间下界""resume 既不补进度也不撤销原承诺",即空闲通知不得提高源端安全时间下界;但实现里 idle 等价于把该分区的下界从 min 中摘掉。
- `joiner.py:44-48`:`_collect` 用 `cut = sent[other]` 淘汰候选(L 行 `t+width < cut` 即删)。`sent` 一旦被 idle 抬高,删除即不可逆。
- `progress.py:23` 的 `max` 使 `sent` 单调,resume(step 15)不会把它拉回:R0 恢复后 `sent[R]` 仍是 100,尽管 R0 的真实承诺 `floor[R0]=0`(`progress.py:13-14` 只被 advance 改写)。

### 缺陷B:检查点转换丢失 idle 分区承诺

- `progress.py:30`:`export()` 的 `floor` 只保留**非 idle** 分区 → checkpoint `idle`(step 14)的载荷里 `floor` 为 `{L0:0,L1:0,R1:100}`,R0 的承诺(0)被丢弃。
- `progress.py:38`:`load()` 对缺失分区用 `result.sent[k[0]]` 回填 → 恢复后 `floor[R0]` 从 0 变为 100(在途观测见 `evidence.json` 的 `checkpoint_conversion.idle`)。CONTRACT.md 第 10 行要求"检查点恢复应保留原始承诺与所有未来合法输入的业务效果",此处两者皆失。
- `checkpoint.py:22`:摘要只校验载荷字节完整("摘要验证通过仅说明载荷未损坏"),对这种语义性丢失完全透明;且 `replay.py:18-25` 的 checkpoint/restore 轨迹项都取自 `export()`,**被丢弃的 floor 在轨迹里也不可见**,只能在途观测确认。
- `progress.py:26-27` + `joiner.py:18-22`:恢复后 `accepts` 用被回填的 floor 拒收合法记录,`apply` 直接 return,记录永不入行、永不匹配。

## 3. 时间线(录制 step)

| step | op | 结果 |
|---|---|---|
| 1-10 | 事件 lU,rU,lA1..3,lB1,lB2,lC,lD,rS | 全部接纳;step 2 配出 [lU,rU,6] |
| 11 | checkpoint `before` | 状态完整(floor 全 0,无 idle),恢复无损(对照) |
| 12 | idle R0 | `sent[R]` 不变(0);R0 真实承诺仍为 0 |
| **13** | **advance R1→100** | **活跃集合只剩 R1 → `sent[R]`=100;同步淘汰 lU,lA1,lA2,lA3(cut=100)。首次不可逆丢失** |
| 14 | checkpoint `idle` | 载荷丢失 `floor[R0]`;`--restore-at idle` 在此重建,`floor[R0]` 0→100 |
| 15 | resume R0 | `sent[R]` 仍为 100(max 单调);live/before 的 floor 正确,idle 恢复的 floor[R0]=100 |
| 16 | rA(R0,A,t=25) | 合法(25≥0)。live/before:接纳但候选已失,**A 键 3 对漏配**;idle:**被拒收**(25<100) |
| 17 | rB(R0,B,t=97) | live/before:接纳并配出 [lB1,rB,11]、[lB2,rB,-11];idle:**被拒收**(97<100),B 键 2 对漏配 |
| 18-20 | rC,rD,lS | 正常配出 [lC,rC,0]、[lD,rD,13]、[lS,rS,6] |
| 21-24 | advance →130 | 淘汰 lB1..lD、lS(step 22)与全部 R 行(step 24);此时双侧 floor 均为 130,淘汰合法(区间分析为空) |

## 4. 三个执行的逐对明细

期望明细(全部录制事件决定,9 对,total=25):
`[lA1,rA,7] [lA2,rA,-7] [lA3,rA,0] [lB1,rB,11] [lB2,rB,-11] [lC,rC,0] [lD,rD,13] [lS,rS,6] [lU,rU,6]`

| 执行 | 对数 | total | 漏配(逐对) | 误配/错值 | 拒收 |
|---|---|---|---|---|---|
| 不中断 live | 6 | 25 | [lA1,rA,7]、[lA2,rA,-7]、[lA3,rA,0] | 无 | 无 |
| before 恢复 | 6 | 25 | 同上 3 对 | 无 | 无 |
| idle 恢复 | 4 | 25 | 上述 3 对 + [lB1,rB,11]、[lB2,rB,-11] | 无 | rA、rB |

三种机制分列(无任何误配或数值错误,只有漏配;已输出对的差值全部正确):

1. **候选淘汰(candidate_evicted)**:step 13 删 lU/lA1/lA2/lA3 → A 键 3 对在三执行中全丢。首次不可逆丢失即此。
2. **检查点转换(checkpoint_conversion)**:step 14 恢复时 `floor[R0]` 0→100(仅 idle 恢复;before 恢复前后 floor 完全一致,见 `checkpoint_conversion.before.changed_by_restore={}`)。
3. **接纳拒收(admission_rejected)**:转换的直接后果——idle 恢复在 step 16、17 拒收合法记录 rA、rB → B 键 2 对仅在该执行丢失(A 键在该执行本就因淘汰不可配,拒收是叠加)。

**已经错误的输出**与**尚未出现但已失去的机会**分开记录于 `evidence.json`:前者为 `per_mode.*.missing_pairs`(上表);后者为 `eviction_intervals` / `resume_analysis` 中没有真实见证的区间,如 lU 的 [5,15](lU 已在 step 2 配过 rU,仍按"覆盖已配过其他记录的候选"核定;此后包内再无 U 键记录,属纯潜在损失)。

## 5. 必要触发条件

缺陷A(三执行共有)缺一不可:

- T1 分区进入 idle(step 12,idle R0);
- T2 同侧另一分区在其空闲期内推进 advance,使 `min(活跃 floor)` 超过 idle 分区的真实承诺(step 13,R1→100 而 R0=0);
- T3 关联器按 `sent[other]` 淘汰候选(`joiner.py:44-48`),删除越过 idle 分区真实视野的候选;
- T4 idle 分区 resume 后合法补送 `t ∈ [真实floor, sent)` 的记录(step 16,rA t=25∈[0,100))。

缺陷B(仅 idle 恢复)追加:

- T5 在"分区 idle 且 `sent[side]` 高于其真实 floor"的窗口内写检查点(step 14),`progress.py:30` 丢弃该 floor;
- T6 从该检查点恢复(`replay.py:20-25`),`progress.py:38` 用 `sent` 回填;
- T7 恢复后该分区合法记录 `t < 回填值` 被 `progress.py:27` 拒收。

`data/normal.json` 之所以安全:idle 发生在两分区都 advance 到 20 之后,`sent[R]`(20)有真实承诺支撑,且 checkpoint `safe` 时 `floor[R0]`(20)恰等于 `sent[R]`(20),回填无损——两个缺陷都被数值巧合掩盖。

## 6. 区间分析(已见记录 × 一条合法后续记录)

规则:对每次候选淘汰与恢复后首次 resume(step 15,分区 R0),假设下一条**新 id、同 key** 的对侧记录来自指定分区,允许先 resume、不再 advance;用**原始安全下界**(仅由 advance 承诺构成)给出仍合法、本可匹配、当前实现却不再能输出的整数时间闭区间。空区间不输出;相邻边界已合并为单一闭区间,不逐整数展开。

### 6.1 候选淘汰(step 13,cut=100;三执行相同)

| 记录 | key | t | 对侧分区 | 失去区间 | 真实见证 |
|---|---|---|---|---|---|
| lU | U | 5 | R0 | **[5,15]** | 无(纯潜在) |
| lA1 | A | 20 | R0 | **[20,30]** | rA t=25(step 16) |
| lA2 | A | 21 | R0 | **[21,31]** | rA t=25(step 16) |
| lA3 | A | 22 | R0 | **[22,32]** | rA t=25(step 16) |

同一淘汰对 R1 的区间为空(R1 已承诺 floor=100,与匹配窗不相交),不输出——说明该淘汰对 R1 合法、仅对未承诺的 R0 构成损失。step 22(cut=130)与 step 24(cut=130)的全部淘汰区间均为空(双侧 floor 已达 130,匹配窗 [t,t+10]/[t-10,t] 与 [130,+∞) 不相交),属合法淘汰,不输出。lU 已配过 rU、rS 已配过 lS,仍按规则覆盖核定。

### 6.2 恢复后首次 resume(step 15,分区 R0;按此前已见 L 记录逐条)

| 执行 | 记录 | 失去区间 | 机制 | 真实见证 |
|---|---|---|---|---|
| live / before | lU,lA1,lA2,lA3 | [5,15]、[20,30]、[21,31]、[22,32] | candidate_evicted(同 6.1,floor 未被转换) | rA t=25 |
| idle | lU,lA1,lA2,lA3 | 同上 | candidate_evicted | rA t=25 |
| idle | **lB1,lB2** | **[95,99]** | admission_rejected(记录仍留存,但恢复后 floor[R0]=100 拒收 t<100) | rB t=97(step 17) |
| idle | lC,lD | 空(匹配窗 [100,110]、[110,120] 全在回填值之上) | — | — |

live/before 中留存记录(lB1..lD)区间为空:floor 未被转换,合法记录仍可接纳并匹配。

### 6.3 端点充分性与无遗漏

- 匹配约束本身是闭区间:对 L 行(t),对侧 R 记录可配当且仅当 `t ∈ [t, t+width]`(R 行对称,`[t-width, t]`)。
- 合法性约束也是半直线:CONTRACT.md 第 8-9 行,唯一时间承诺来自 advance(`t ≥ floor`,初值 0),idle/resume 不产生下界;故"仍合法"= `t ≥ 原始floor`。
- 两类约束的交仍是单一闭区间 `[max(窗下界, 原始floor), 窗上界]`(下界再与 0 取 max);区间内每个整数都同时满足合法与可配,区间外要么不合法、要么本就不配——故端点充分且无遗漏,相邻整数天然并入同一闭区间。
- 淘汰场景下"不再能输出"对整区间成立(候选已删),无需再裁剪;拒收场景下留存记录在 `t ≥ 恢复后floor` 时仍可输出,故失去的区间是 `[max(窗下界,原始floor), 恢复后floor − 1]`,同样是单一闭区间(恢复后floor ≤ 窗下界时为空,如 lC/lD)。
- 只涉及已见记录与一条新对侧记录,未分析两条未出现记录的组合。

## 7. 对照实验(仅移动通知/恢复位置,事件内容、相对顺序、代码不变)

| 对照 | 构造 | 结果(live / before / idle) | 结论 |
|---|---|---|---|
| `derived/idle_after_advance.json` | idle(R0) 推迟到 advance(R1→100) 之后 | 6 / 6 / 4,首次淘汰仍在 step 13(改由 idle 自身触发) | **残余故障必须保留**:idle 通知本身就会把 idle 分区挤出 `min`(progress.py:19-23),调整顺序不消除缺陷 |
| `derived/advance_after_resume.json` | advance(R1→100) 推迟到 resume(R0) 之后 | **9 / 9 / 9**,首次淘汰推迟到 step 21(合法) | 触发条件 T2 被移除后三执行全部正确,证明 T1-T4 是必要组合;同时 checkpoint `idle` 时 `sent[R]`=0=`floor[R0]`,回填无损,缺陷B 同步消失 |
| `--restore-at before` | 改用另一现有检查点恢复 | 6 对,同 live | 恢复本身无损(`changed_by_restore={}`);隔离出缺陷B 只由 checkpoint `idle` 的转换引入 |
| `checks.py normal` | 正常包 + safe 恢复 | 退出 0,`NORMAL_REGRESSION_OK` | 正常回归保留通过 |

保留的对照事实(`evidence.json` 的 `controls`):无关分区键 U(L0/R1)、S(L1/R1)的对在三执行中都在;零差对 [lC,rC,0] 三执行都在、[lA3,rA,0] 三执行都丢;C/D 键虽经 R0 送达,但 t≥100 高于被抬升的 `sent[R]`,故不受影响——进一步印证损失边界正好由区间 [真实floor, sent) 刻画。

## 8. 零和掩盖

期望 total=25,三执行实际 total 都是 25,**按总和监控完全不可见**:

- live/before 漏配的 A 键 3 对:7 + (−7) + 0 = **0**;
- idle 恢复额外漏配的 B 键 2 对:11 + (−11) = **0**。

同 key 多对多关联下,正负差成对设计(v: 7/−7/0、11/−11)使漏配组差值和为零;另有零差对 [lA3,rA,0] 丢失对总和也无影响。对数(9→6→4)与明细才暴露故障,total 不能代替明细核对。

## 9. 证据与复现

- `evidence.json` 结构:`original_entries`(checks.py normal、连续两次 `checks.py` incident 均退出 1 且输出 `JOIN_IDLE_STATE_LOSS`、三模式 replay.py 原始输出、`data/incident.log` 记录的两轮同标记失败)、`expected`(独立业务计算)、`per_mode`(逐对差异/拒收/淘汰)、`first_irreversible_loss`、`checkpoint_conversion`(写入前/导出/恢复后三份 floor)、`eviction_intervals` 与 `resume_analysis`(区间端点与真实见证)、`derived_bundles`(派生包全文与三模式结果)、`zero_sum_masking`、`controls`、`source_refs`(行号+行文本)。
- 复现:`python3 analysis.py run data/incident.json` 重算并写证据;`python3 analysis.py verify data/incident.json` 不用旧缓存重算并核对该文件与本报告数值、见证(一致退出 0);`python3 analysis.py demo data/incident.json` 输出真实明细差异与区间见证(rA@25∈[20,30]、rB@97∈[95,99])。
- 运行清单 `.pointwise-runtime.json` 的 verify/demo 已指向上述两个分析入口;normal 断言由 verify 内部保留执行并通过。
- 不要求修复;缺陷定位见第 2 节(progress.py:19-23、29-31、33-40;joiner.py:44-48)。

<!-- report-facts -->
```json
{
 "checkpoint_floor_changes": {
  "before": {},
  "idle": {
   "R0": {
    "committed": 0,
    "restored": 100
   }
  }
 },
 "derived": {
  "advance_after_resume": {
   "first_evict_step": 21,
   "pairs_per_mode": {
    "before": 9,
    "idle": 9,
    "live": 9
   }
  },
  "idle_after_advance": {
   "first_evict_step": 13,
   "pairs_per_mode": {
    "before": 6,
    "idle": 4,
    "live": 6
   }
  }
 },
 "eviction_intervals": [
  {
   "interval": [
    5,
    15
   ],
   "partition": "R0",
   "realized_by": [],
   "record": "lU",
   "step": 13
  },
  {
   "interval": [
    20,
    30
   ],
   "partition": "R0",
   "realized_by": [
    {
     "id": "rA",
     "step": 16,
     "t": 25
    }
   ],
   "record": "lA1",
   "step": 13
  },
  {
   "interval": [
    21,
    31
   ],
   "partition": "R0",
   "realized_by": [
    {
     "id": "rA",
     "step": 16,
     "t": 25
    }
   ],
   "record": "lA2",
   "step": 13
  },
  {
   "interval": [
    22,
    32
   ],
   "partition": "R0",
   "realized_by": [
    {
     "id": "rA",
     "step": 16,
     "t": 25
    }
   ],
   "record": "lA3",
   "step": 13
  }
 ],
 "expected_pairs": [
  [
   "lA1",
   "rA",
   7
  ],
  [
   "lA2",
   "rA",
   -7
  ],
  [
   "lA3",
   "rA",
   0
  ],
  [
   "lB1",
   "rB",
   11
  ],
  [
   "lB2",
   "rB",
   -11
  ],
  [
   "lC",
   "rC",
   0
  ],
  [
   "lD",
   "rD",
   13
  ],
  [
   "lS",
   "rS",
   6
  ],
  [
   "lU",
   "rU",
   6
  ]
 ],
 "expected_total": 25,
 "first_irreversible_loss_step": 13,
 "modes": {
  "before": {
   "pairs": [
    [
     "lB1",
     "rB",
     11
    ],
    [
     "lB2",
     "rB",
     -11
    ],
    [
     "lC",
     "rC",
     0
    ],
    [
     "lD",
     "rD",
     13
    ],
    [
     "lS",
     "rS",
     6
    ],
    [
     "lU",
     "rU",
     6
    ]
   ],
   "rejected": [],
   "total": 25
  },
  "idle": {
   "pairs": [
    [
     "lC",
     "rC",
     0
    ],
    [
     "lD",
     "rD",
     13
    ],
    [
     "lS",
     "rS",
     6
    ],
    [
     "lU",
     "rU",
     6
    ]
   ],
   "rejected": [
    "rA",
    "rB"
   ],
   "total": 25
  },
  "live": {
   "pairs": [
    [
     "lB1",
     "rB",
     11
    ],
    [
     "lB2",
     "rB",
     -11
    ],
    [
     "lC",
     "rC",
     0
    ],
    [
     "lD",
     "rD",
     13
    ],
    [
     "lS",
     "rS",
     6
    ],
    [
     "lU",
     "rU",
     6
    ]
   ],
   "rejected": [],
   "total": 25
  }
 },
 "resume_intervals_idle_mode": [
  {
   "interval": [
    5,
    15
   ],
   "mechanism": "candidate_evicted",
   "realized_by": [],
   "record": "lU"
  },
  {
   "interval": [
    20,
    30
   ],
   "mechanism": "candidate_evicted",
   "realized_by": [
    {
     "id": "rA",
     "step": 16,
     "t": 25
    }
   ],
   "record": "lA1"
  },
  {
   "interval": [
    21,
    31
   ],
   "mechanism": "candidate_evicted",
   "realized_by": [
    {
     "id": "rA",
     "step": 16,
     "t": 25
    }
   ],
   "record": "lA2"
  },
  {
   "interval": [
    22,
    32
   ],
   "mechanism": "candidate_evicted",
   "realized_by": [
    {
     "id": "rA",
     "step": 16,
     "t": 25
    }
   ],
   "record": "lA3"
  },
  {
   "interval": [
    95,
    99
   ],
   "mechanism": "admission_rejected",
   "realized_by": [
    {
     "id": "rB",
     "step": 17,
     "t": 97
    }
   ],
   "record": "lB1"
  },
  {
   "interval": [
    95,
    99
   ],
   "mechanism": "admission_rejected",
   "realized_by": [
    {
     "id": "rB",
     "step": 17,
     "t": 97
    }
   ],
   "record": "lB2"
  }
 ],
 "zero_sum_lost_groups": {
  "before": {
   "A": {
    "pairs": [
     [
      "lA1",
      "rA",
      7
     ],
     [
      "lA2",
      "rA",
      -7
     ],
     [
      "lA3",
      "rA",
      0
     ]
    ],
    "sum": 0
   }
  },
  "idle": {
   "A": {
    "pairs": [
     [
      "lA1",
      "rA",
      7
     ],
     [
      "lA2",
      "rA",
      -7
     ],
     [
      "lA3",
      "rA",
      0
     ]
    ],
    "sum": 0
   },
   "B": {
    "pairs": [
     [
      "lB1",
      "rB",
      11
     ],
     [
      "lB2",
      "rB",
      -11
     ]
    ],
    "sum": 0
   }
  },
  "live": {
   "A": {
    "pairs": [
     [
      "lA1",
      "rA",
      7
     ],
     [
      "lA2",
      "rA",
      -7
     ],
     [
      "lA3",
      "rA",
      0
     ]
    ],
    "sum": 0
   }
  }
 }
}
```
