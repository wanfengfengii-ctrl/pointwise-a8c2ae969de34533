# 双路采样关联事故包

本包只有一条链路：四个源分区的进度进入关联器，关联状态与已发明细写入SQLite检查点，恢复后继续消费同一录制包。诊断期间不改progress.py、joiner.py、checkpoint.py、replay.py、checks.py及data/原始材料。

## 业务语义
固定L、R两路，每路分区0、1；width为非负整数。全部时间、数值为整数，时间非负，id在整个包内唯一。数据已校验，分析器无须开发非法格式处理。ops按数组顺序执行，step从1计数。
event含op、side、part、id、key、t、v。全部同key记录作多对多关联：L.t <= R.t <= L.t+width，每对只输出一次，输出[L.id,R.id,L.v-R.v]，按两个id排序。负差、零差均保留，total是差值总和。记录可在各分区安全下界之上乱序到达，不能用最大已见事件时间替代安全下界。
advance含side、part、floor，表示此分区以后交付的事件t >= floor；初值0，同分区单调。所有原始包及诊断派生包都须遵守这一承诺。
idle、resume含side、part，通知该分区暂时无输入、重新活跃；idle到resume之间没有该分区事件。空闲期间可以不让它阻塞展示进度，但idle本身不承诺未来时间下界。resume既不补进度也不撤销原承诺。
checkpoint含name，名字在包内唯一。检查点恢复应保留原始承诺与所有未来合法输入的业务效果。摘要验证通过仅说明载荷未损坏。事件只投递一次；不考虑源重发、并发、坏包、进程被杀或外部系统。
最终正确明细由全部录制事件决定；进度和检查点不产生、撤回或修改业务匹配。

## 已有入口与样例
python3 replay.py data/incident.json
python3 replay.py data/incident.json --restore-at idle
输出一个JSON对象，含pairs、total、retained、trace。trace给出每步接纳、进度、淘汰、匹配和视图，checkpoint项含写入前状态及摘要，restore项含恢复状态。--restore-at NAME在对应检查点写入后立即重建关联器，只继续该点之后的操作；不指定时不中断。--db PATH可保留SQLite检查点，默认使用内存SQLite；Checkpoints.read(name)返回已核摘要的[step,state]。重放不读取历史库中的检查点，始终从输入包开头执行并覆盖同名记录。
python3 checks.py normal应退出0并输出NORMAL_REGRESSION_OK；正常包的pairs为[["l0","r0",6],["l1","r1",-2],["l2","r2",0]]、total为4，不中断与safe检查点恢复均成立。
python3 checks.py incident分别核对不中断、before恢复、idle恢复与全事件关联明细；故障时退出1并输出JOIN_IDLE_STATE_LOSS。data/incident.log是连续两次运行的业务stdout及退出值，不是诊断结论。checks.expected只作有限包最终明细对照，不计算因果归属或潜在后续损失。

## 本轮诊断交付
交付REPORT.md、analysis.py、evidence.json。报告引用实际源码行号和录制step，说明必要触发条件与首次不可逆丢失；逐对列出已出现的误配、漏配及数值差异，区分接纳拒绝、候选淘汰、检查点转换，不能以总和代替明细。
分析不中断、before恢复、idle恢复三个执行，分开记录已经错误的输出与尚未出现但已失去的匹配机会。对轨迹中的每次候选淘汰，以及恢复后首次resume的各分区，按此前已见记录逐条核定潜在影响：假设下一条新id、同key的对侧记录来自指定分区，允许先resume，但不再advance；用原始安全下界给出所有仍合法且本可匹配、当前实现却不再能输出的整数时间闭区间。覆盖已配过其他记录的候选，空区间不输出；边界相邻可合并，不逐整数展开。只分析已见记录与一个新对侧记录，不分析两个尚未出现记录的组合。说明区间端点为何充分且无遗漏。
仅改变通知或恢复位置构造可重跑对照：分别推迟idle到进度通知之后、推迟该次进度通知到分区恢复之后，以及改用另一现有检查点恢复；不得改变事件内容、事件相对顺序或代码。如改变通知顺序后仍有故障，必须保留并解释残余，不能把少一种症状当成全部消失。保留同key零和、零差、无关分区以及安全恢复对照。
analysis.py run BUNDLE重新执行上述原入口并独立计算期望和区间，写evidence.json；analysis.py verify BUNDLE不依赖旧计算缓存，重算并核对该文件及报告的数值和见证；analysis.py demo BUNDLE展示真实明细差异和至少一个区间见证。分析入口只接收本事故包或保持检查点名称的上述派生包，不要求处理任意新业务包。三者向stdout输出JSON对象，verify一致时退出0，不一致时非0。证据JSON内部结构由交付者说明，需保存原入口输出、派生包、源码引用与区间端点，不能只有结论或静态成功文字。更新运行清单的verify、demo以调用这两个分析入口，normal断言保持通过，并留存原故障连续两次同标记失败的证据。不要求修复或提出完整替代架构。
