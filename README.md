# 网络策略变更与连通性仲裁台

离线模拟集群内服务互访的意图审批、网络策略裁定和节点下发核验。只处理东西向通信，不处理 DNS 解析或租户出站授权。Python 标准库和 SQLite 即可运行。

`intent` 保存服务 UID、租户、服务端口协议契约与调用意图；`policy` 用默认拒绝和规则优先级核对必需路径与越权放行；`enforce` 固定下发节点成员，收集代际回执和连接事实，出现反例时建立回滚案。事件重放会对照全部业务表。

```sh
python -m netcourt demo --db demo.sqlite
python -m netcourt replay --db demo.sqlite
python -m unittest discover -s tests -v
```

各入口单独运行：`python -m netcourt intent|policy|enforce --db state.sqlite --fixture fixtures/<入口>_normal.json`。每个入口还有 `_failure.json`；三个正常样例须按意图、策略、下发的顺序在同一新数据库执行。

## 网络授时交付（`time_delivery`）

`time_note` 只是用平均偏移给出的临时合格提示：正、负相反的样本会互相抵消成假准确。`time_delivery` 改用闭合误差区间，绝不把相反偏移平均成零。

- 每次校时（`calibrate`）按 NTP 双向交换（t1–t4、往返时延、非对称误差界）算出 `[lo, hi]` 区间，并固化来源认证链、测量路径、路径故障段、振荡器状态、跳秒表与测量时段。
- 正式裁决（`adjudicate`）按认证根/故障域划分独立组：共享同一授时根的来源可能被一起欺骗，自洽的一组样本不算佐证；不同独立组区间不相交判 `inconsistent`，相交且满足高精度限才判 `accurate`。非对称未校验时误差界退化为半 RTT，稳定 RTT 无法藏住真实误差。
- 断链后节点振荡器进入 holdover，区间按漂移率随时间向两侧增长，超过保时预算即拒绝给界；跨机房时钟关系在迁移与断链期间持续投影上界，无法证明先后的交易由 `order_events` 标为 `unsortable`，不强行输出精确时间。
- 时间证明（`issue_proof`）签发时冻结当时误差界与原始样本；高精度可短时暂停（请求排队），低精度须连续。任何降级都要求客户可核验的选择（`record_election`/`verify_election`，带 SHA-256 摘要与有效期），并记账补偿。来源事后被判欺骗或跳秒处理错误（`condemn_source`）或撤回（`withdraw_proof`）时，原证明不被改写，而是签发指向旧证明的更正并列出需重审的交易和可追溯失效区间。断链后迟到的样本只作法证，不能重开已封存裁决，与已签证明矛盾时只发更正。

