# 网络策略变更与连通性仲裁台

离线模拟集群内服务互访的意图审批、网络策略裁定和节点下发核验。只处理东西向通信，不处理 DNS 解析或租户出站授权。Python 标准库和 SQLite 即可运行。

`intent` 保存服务 UID、租户、服务端口协议契约与调用意图；`policy` 用默认拒绝和规则优先级核对必需路径与越权放行；`enforce` 固定下发节点成员，收集代际回执和连接事实，出现反例时建立回滚案。事件重放会对照全部业务表。

```sh
python -m netcourt demo --db demo.sqlite
python -m netcourt replay --db demo.sqlite
python -m unittest discover -s tests -v
```

各入口单独运行：`python -m netcourt intent|policy|enforce --db state.sqlite --fixture fixtures/<入口>_normal.json`。每个入口还有 `_failure.json`；三个正常样例须按意图、策略、下发的顺序在同一新数据库执行。
