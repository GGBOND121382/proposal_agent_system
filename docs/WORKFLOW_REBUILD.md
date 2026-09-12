# Workflow Rebuild

## 日常只用这两条命令

```bash
python scripts/rebuild.py <workflow-id>
```

从指定 workflow 开始重建，并自动沿当前分支重建所有下游 workflow。旧的已完成 workflow 保留不动，新分支的 prerequisite 自动关联。

如果执行停在 Gate、配置等待或失败节点，处理原因后运行：

```bash
python scripts/rebuild.py resume <rebuild-operation-id>
```

`resume` 会继续同一 rebuild branch；如果当前节点是 `BLOCKED_*`，会自动创建该节点的新实例并继续，下游关联无需手工处理。

## 只重建当前节点

仅在确实不希望重建下游时使用：

```bash
python scripts/rebuild.py <workflow-id> --self-only
```

## 固定规则

- `COMPLETED` 源 workflow 永不修改；重建生成新版本。
- 未完成/阻断的源 workflow 才会被 `RESTART` 并取消旧实例。
- 原 workflow 的 `prerequisite_workflow_ids` 永不重绑。
- 新分支只使用 RebuildPlan 显式冻结的 prerequisite，不在执行途中重新查询 `latest`。
- 若某个旧 prerequisite 同时被本次重建，则下游自动指向它的新版本；否则继续绑定原来的 frozen prerequisite。
