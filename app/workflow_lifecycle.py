from __future__ import annotations

import copy
import json
from typing import Any

from .util import new_id, utc_now
from .workflow_defs import WORKFLOWS
from .workflow_status import (
    WorkflowStatus,
    ensure_transition,
    is_recoverable_block,
    occupies_workflow_slot,
)


REBUILD_SCOPE_SELF = "SELF"
REBUILD_SCOPE_ALL_DOWNSTREAM = "ALL_DOWNSTREAM"
REBUILD_OPERATION_STATUSES = {
    "PLANNED",
    "RUNNING",
    "PAUSED",
    "COMPLETED",
    "FAILED",
    "ABORTED",
}


class WorkflowLifecycleService:
    """Deterministic workflow restart/rebuild orchestration.

    Dependency edges remain frozen on every workflow instance. Rebuild creates a
    new branch and records lineage separately; it never silently rebinds an old
    downstream workflow to a newer upstream workflow.
    """

    def __init__(self, db, workflows):
        self.db = db
        self.workflows = workflows

    @staticmethod
    def _state(row: dict[str, Any]) -> dict[str, Any]:
        if isinstance(row.get("state"), dict):
            return copy.deepcopy(row["state"])
        return json.loads(row.get("state_json") or "{}")

    def _workflow_row(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if row is None:
            raise KeyError(f"Workflow not found: {workflow_id}")
        return row

    def _project_workflows(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM workflows WHERE project_id=? ORDER BY created_at ASC, id ASC",
            (project_id,),
        )
        for row in rows:
            row["state"] = self._state(row)
        return rows

    @staticmethod
    def _bindings(row: dict[str, Any]) -> dict[str, str]:
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        value = state.get("prerequisite_workflow_ids")
        if not isinstance(value, dict):
            return {}
        return {
            str(key): str(workflow_id)
            for key, workflow_id in value.items()
            if str(key).strip() and str(workflow_id).strip()
        }

    @staticmethod
    def _is_parent_workflow(row: dict[str, Any]) -> bool:
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        return not bool(state.get("parent_workflow_id"))

    def _require_frozen_source_bindings(self, row: dict[str, Any]) -> dict[str, str]:
        state = row["state"]
        bindings = self._bindings(row)
        required = set(
            self.workflows.runtime._required_workflow_types(
                row["project_id"],
                row["workflow_type"],
                state.get("options") or {},
            )
        )
        if required and "prerequisite_workflow_ids" not in state:
            raise ValueError(
                f"源工作流 {row['id']} 没有冻结的 prerequisite_workflow_ids；"
                "为避免把历史分支误绑定到当前 latest，不能自动猜测。"
            )
        missing = sorted(required - set(bindings))
        if missing:
            raise ValueError(
                f"源工作流 {row['id']} 的冻结前置绑定不完整：" + "、".join(missing)
            )
        return bindings

    def _select_branch_nodes(
        self,
        root: dict[str, Any],
        *,
        scope: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if scope not in {REBUILD_SCOPE_SELF, REBUILD_SCOPE_ALL_DOWNSTREAM}:
            raise ValueError(f"unsupported rebuild scope: {scope}")
        if root["workflow_type"] not in WORKFLOWS:
            raise ValueError(
                f"{root['workflow_type']} 暂不支持标准 lineage rebuild；仅支持数据库原生 WF-1..WF-5 与 WF-3B_TOPIC_BACKGROUND_RESEARCH。"
            )
        if root["status"] == WorkflowStatus.CANCELLED.value:
            raise ValueError("CANCELLED workflow 不能作为 rebuild 源；请选择其最后一个有效版本。")

        rows = self._project_workflows(root["project_id"])
        by_id = {str(row["id"]): row for row in rows}
        root_id = str(root["id"])
        root = by_id[root_id]
        if scope == REBUILD_SCOPE_SELF:
            return [root], []

        # Branches created by this service have explicit membership. Prefer that
        # identity over timestamp inference on subsequent rebuilds.
        membership = self.db.fetchone(
            """SELECT branch_id,position FROM workflow_branch_members
                 WHERE workflow_id=? ORDER BY created_at DESC LIMIT 1""",
            (root_id,),
        )
        if membership is not None:
            members = self.db.fetchall(
                """SELECT workflow_id,position FROM workflow_branch_members
                     WHERE branch_id=? AND position>=? ORDER BY position ASC,created_at ASC""",
                (membership["branch_id"], int(membership["position"])),
            )
            selected = [
                by_id[str(item["workflow_id"])]
                for item in members
                if str(item["workflow_id"]) in by_id
                and by_id[str(item["workflow_id"])]["status"] != WorkflowStatus.CANCELLED.value
            ]
            if selected and str(selected[0]["id"]) == root_id:
                return selected, []

        # Legacy workflows predate branch IDs. Build the concrete consumed-by
        # graph, choose the newest leaf, then walk its exact frozen prerequisite
        # ancestry back to the requested root. This avoids mixing independent
        # historical WF-4/WF-5 branches merely because they share a workflow type.
        descendants: set[str] = set()
        frontier = {root_id}
        while frontier:
            next_frontier: set[str] = set()
            for row in rows:
                workflow_id = str(row["id"])
                if workflow_id == root_id or workflow_id in descendants:
                    continue
                if row["workflow_type"] not in WORKFLOWS:
                    continue
                if row["status"] == WorkflowStatus.CANCELLED.value:
                    continue
                if not self._is_parent_workflow(row):
                    continue
                if set(self._bindings(row).values()) & frontier:
                    descendants.add(workflow_id)
                    next_frontier.add(workflow_id)
            frontier = next_frontier

        if not descendants:
            return [root], []

        graph_ids = {root_id, *descendants}
        outgoing: dict[str, set[str]] = {workflow_id: set() for workflow_id in graph_ids}
        for child_id in descendants:
            for prerequisite_id in self._bindings(by_id[child_id]).values():
                if prerequisite_id in graph_ids:
                    outgoing[prerequisite_id].add(child_id)
        leaves = [workflow_id for workflow_id in descendants if not outgoing[workflow_id]]
        leaf_id = max(
            leaves,
            key=lambda workflow_id: (
                str(by_id[workflow_id].get("updated_at") or ""),
                str(by_id[workflow_id].get("created_at") or ""),
                workflow_id,
            ),
        )

        chosen: set[str] = {root_id, leaf_id}
        stack = [leaf_id]
        while stack:
            workflow_id = stack.pop()
            for prerequisite_id in self._bindings(by_id[workflow_id]).values():
                if prerequisite_id in graph_ids and prerequisite_id not in chosen:
                    chosen.add(prerequisite_id)
                    stack.append(prerequisite_id)

        incoming: dict[str, set[str]] = {workflow_id: set() for workflow_id in chosen}
        chosen_outgoing: dict[str, set[str]] = {workflow_id: set() for workflow_id in chosen}
        for child_id in chosen:
            for prerequisite_id in self._bindings(by_id[child_id]).values():
                if prerequisite_id in chosen:
                    incoming[child_id].add(prerequisite_id)
                    chosen_outgoing[prerequisite_id].add(child_id)
        ready = sorted(
            [workflow_id for workflow_id, deps in incoming.items() if not deps],
            key=lambda workflow_id: (0 if workflow_id == root_id else 1, workflow_id),
        )
        ordered_ids: list[str] = []
        while ready:
            workflow_id = ready.pop(0)
            ordered_ids.append(workflow_id)
            for child_id in sorted(chosen_outgoing[workflow_id]):
                incoming[child_id].discard(workflow_id)
                if not incoming[child_id] and child_id not in ordered_ids and child_id not in ready:
                    ready.append(child_id)
            ready.sort(key=lambda item: (0 if item == root_id else 1, item))
        if len(ordered_ids) != len(chosen):
            raise ValueError("workflow dependency graph contains a cycle; rebuild refused")
        if ordered_ids[0] != root_id:
            ordered_ids.remove(root_id)
            ordered_ids.insert(0, root_id)
        ignored = sorted(descendants - chosen)
        return [by_id[workflow_id] for workflow_id in ordered_ids], ignored

    def _assert_no_unrelated_active_slot_conflicts(
        self,
        nodes: list[dict[str, Any]],
    ) -> None:
        selected_ids = {str(item["id"]) for item in nodes}
        selected_types = {str(item["workflow_type"]) for item in nodes}
        rows = self._project_workflows(nodes[0]["project_id"])
        conflicts: list[str] = []
        for row in rows:
            if str(row["workflow_type"]) not in selected_types:
                continue
            if str(row["id"]) in selected_ids:
                continue
            if not self._is_parent_workflow(row):
                continue
            if occupies_workflow_slot(str(row["status"])):
                conflicts.append(f"{row['workflow_type']}:{row['id']}({row['status']})")
        if conflicts:
            raise ValueError(
                "存在不属于本次源分支的活动工作流，无法安全创建并行 rebuild 分支："
                + "、".join(sorted(conflicts))
            )

    def _build_plan(self, workflow_id: str, *, scope: str) -> dict[str, Any]:
        root = self._workflow_row(workflow_id)
        root["state"] = self._state(root)
        nodes, ignored = self._select_branch_nodes(root, scope=scope)
        self._assert_no_unrelated_active_slot_conflicts(nodes)

        plan_nodes: list[dict[str, Any]] = []
        for index, row in enumerate(nodes):
            bindings = self._require_frozen_source_bindings(row)
            source_status = str(row["status"])
            if source_status == WorkflowStatus.COMPLETED.value:
                relation_type = "RERUN_OF" if index == 0 else "REBUILD_OF"
            else:
                relation_type = "RESTART_OF"
            options = copy.deepcopy(row["state"].get("options") or {})
            options.pop("idempotency_key", None)
            plan_nodes.append(
                {
                    "position": index,
                    "source_workflow_id": str(row["id"]),
                    "workflow_type": str(row["workflow_type"]),
                    "source_status": source_status,
                    "relation_type": relation_type,
                    "lineage_parent_workflow_id": str(row["id"]),
                    "current_relation_type": relation_type,
                    "restart_history": [],
                    "source_prerequisite_workflow_ids": bindings,
                    "options": options,
                    "new_workflow_id": None,
                    "new_status": None,
                    "cancelled_source": False,
                }
            )
        return {
            "schema_version": "1.0",
            "root_source_workflow_id": str(root["id"]),
            "project_id": str(root["project_id"]),
            "scope": scope,
            "binding_policy": "FROZEN_BRANCH_REMAP",
            "nodes": plan_nodes,
            "ignored_older_downstream_consumers": ignored,
        }

    def _create_operation(self, plan: dict[str, Any]) -> dict[str, Any]:
        operation_id = new_id("rebuild")
        branch_id = new_id("branch")
        now = utc_now()
        self.db.execute(
            """INSERT INTO workflow_rebuild_operations(
                   id,project_id,branch_id,root_source_workflow_id,scope,status,
                   current_index,plan_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                plan["project_id"],
                branch_id,
                plan["root_source_workflow_id"],
                plan["scope"],
                "PLANNED",
                0,
                json.dumps(plan, ensure_ascii=False),
                now,
                now,
            ),
        )
        self.db.audit(
            "WORKFLOW_REBUILD_PLANNED",
            project_id=plan["project_id"],
            object_id=operation_id,
            metadata={
                "branch_id": branch_id,
                "root_source_workflow_id": plan["root_source_workflow_id"],
                "scope": plan["scope"],
                "source_workflow_ids": [item["source_workflow_id"] for item in plan["nodes"]],
                "ignored_older_downstream_consumers": plan["ignored_older_downstream_consumers"],
            },
        )
        return self.get_operation(operation_id)

    def list_operations(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not str(project_id or "").strip():
            return []
        bounded_limit = max(1, min(int(limit), 200))
        rows = self.db.fetchall(
            """SELECT id FROM workflow_rebuild_operations
                 WHERE project_id=? ORDER BY updated_at DESC,created_at DESC LIMIT ?""",
            (project_id, bounded_limit),
        )
        return [self.get_operation(str(row["id"])) for row in rows]

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        row = self.db.fetchone(
            "SELECT * FROM workflow_rebuild_operations WHERE id=?", (operation_id,)
        )
        if row is None:
            raise KeyError(f"Rebuild operation not found: {operation_id}")
        return self._decode_operation(row)

    @staticmethod
    def _decode_operation(row: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(row)
        plan = json.loads(row.pop("plan_json"))
        row["plan"] = plan
        index = int(row["current_index"])
        nodes = plan.get("nodes") or []
        active = (
            nodes[index]
            if row["status"] not in {"COMPLETED", "ABORTED"}
            and 0 <= index < len(nodes)
            else None
        )
        row["active_node"] = copy.deepcopy(active)
        row["created_workflow_ids"] = [
            item["new_workflow_id"] for item in nodes if item.get("new_workflow_id")
        ]
        return row

    def _persist_operation(
        self,
        operation: dict[str, Any],
        *,
        status: str,
        current_index: int,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in REBUILD_OPERATION_STATUSES:
            raise ValueError(f"unknown rebuild operation status: {status}")
        now = utc_now()
        self.db.execute(
            """UPDATE workflow_rebuild_operations
                  SET status=?,current_index=?,plan_json=?,updated_at=?
                WHERE id=?""",
            (
                status,
                current_index,
                json.dumps(plan, ensure_ascii=False),
                now,
                operation["id"],
            ),
        )
        return self.get_operation(operation["id"])

    def _cancel_workflow_for_operation(
        self,
        operation: dict[str, Any],
        workflow_id: str,
        *,
        reason: str,
    ) -> None:
        source_id = str(workflow_id)
        with self.db.transaction() as tx:
            current = tx.fetchone("SELECT * FROM workflows WHERE id=?", (source_id,))
            if current is None:
                raise KeyError(f"Workflow not found: {source_id}")
            state = json.loads(current["state_json"] or "{}")
            if current["status"] == WorkflowStatus.COMPLETED.value:
                return
            if current["status"] == WorkflowStatus.CANCELLED.value:
                marker = state.get("cancelled_for_rebuild") or {}
                if marker.get("operation_id") == operation["id"]:
                    return
                raise ValueError(f"source workflow was cancelled outside this rebuild: {source_id}")
            ensure_transition(current["status"], WorkflowStatus.CANCELLED.value)
            now = utc_now()
            cancelled_gates = tx.execute(
                """UPDATE gates
                      SET status='CANCELLED',decision_json=?,updated_at=?
                    WHERE workflow_id=? AND status='OPEN'""",
                (
                    json.dumps(
                        {
                            "action": "CANCEL",
                            "reason": reason,
                            "rebuild_operation_id": operation["id"],
                        },
                        ensure_ascii=False,
                    ),
                    now,
                    source_id,
                ),
            ).rowcount
            state["cancelled_for_rebuild"] = {
                "cancelled_at": now,
                "operation_id": operation["id"],
                "branch_id": operation["branch_id"],
                "reason": reason,
            }
            tx.update_workflow(
                workflow_id=source_id,
                status=WorkflowStatus.CANCELLED.value,
                current_step=int(current["current_step"]),
                state=state,
                expected_updated_at=current["updated_at"],
            )
            tx.audit(
                "WORKFLOW_CANCELLED_FOR_LINEAGE_RESTART",
                project_id=operation["project_id"],
                object_id=source_id,
                metadata={
                    "operation_id": operation["id"],
                    "branch_id": operation["branch_id"],
                    "cancelled_open_gates": cancelled_gates,
                    "reason": reason,
                },
            )

    def _cancel_source_for_restart(
        self,
        operation: dict[str, Any],
        node: dict[str, Any],
    ) -> None:
        self._cancel_workflow_for_operation(
            operation,
            str(node["source_workflow_id"]),
            reason="workflow lineage restart",
        )

    @staticmethod
    def _remap_bindings(plan: dict[str, Any], node: dict[str, Any]) -> dict[str, str]:
        new_by_source = {
            str(item["source_workflow_id"]): str(item["new_workflow_id"])
            for item in plan.get("nodes") or []
            if item.get("new_workflow_id")
        }
        result: dict[str, str] = {}
        for workflow_type, old_id in (node.get("source_prerequisite_workflow_ids") or {}).items():
            result[str(workflow_type)] = new_by_source.get(str(old_id), str(old_id))
        return result

    def _find_started_child(
        self,
        operation_id: str,
        source_workflow_id: str,
    ) -> str | None:
        rows = self.db.fetchall(
            "SELECT id,status,state_json FROM workflows ORDER BY created_at DESC"
        )
        for row in rows:
            if str(row.get("status") or "") == WorkflowStatus.CANCELLED.value:
                continue
            state = json.loads(row.get("state_json") or "{}")
            lifecycle = state.get("workflow_lifecycle") or {}
            if (
                lifecycle.get("rebuild_operation_id") == operation_id
                and lifecycle.get("source_workflow_id") == source_workflow_id
            ):
                return str(row["id"])
        return None

    def _record_lineage(
        self,
        operation: dict[str, Any],
        node: dict[str, Any],
        child_workflow_id: str,
    ) -> None:
        existing = self.db.fetchone(
            "SELECT * FROM workflow_lineage WHERE child_workflow_id=?",
            (child_workflow_id,),
        )
        if existing is not None:
            if (
                str(existing["parent_workflow_id"]) != str(
                    node.get("lineage_parent_workflow_id") or node["source_workflow_id"]
                )
                or str(existing["operation_id"]) != str(operation["id"])
            ):
                raise ValueError(f"workflow lineage conflict for {child_workflow_id}")
            return
        now = utc_now()
        with self.db.transaction() as tx:
            tx.execute(
                """INSERT INTO workflow_lineage(
                       id,project_id,branch_id,operation_id,parent_workflow_id,
                       child_workflow_id,relation_type,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    new_id("lineage"),
                    operation["project_id"],
                    operation["branch_id"],
                    operation["id"],
                    node.get("lineage_parent_workflow_id") or node["source_workflow_id"],
                    child_workflow_id,
                    node.get("current_relation_type") or node["relation_type"],
                    now,
                ),
            )
            tx.execute(
                """INSERT OR IGNORE INTO workflow_branch_members(
                       branch_id,operation_id,workflow_id,source_workflow_id,position,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    operation["branch_id"],
                    operation["id"],
                    child_workflow_id,
                    node["source_workflow_id"],
                    int(node["position"]),
                    now,
                ),
            )
            tx.audit(
                "WORKFLOW_LINEAGE_CREATED",
                project_id=operation["project_id"],
                object_id=child_workflow_id,
                metadata={
                    "operation_id": operation["id"],
                    "branch_id": operation["branch_id"],
                    "parent_workflow_id": node.get("lineage_parent_workflow_id") or node["source_workflow_id"],
                    "relation_type": node.get("current_relation_type") or node["relation_type"],
                },
            )

    async def rebuild(
        self,
        workflow_id: str,
        *,
        scope: str = REBUILD_SCOPE_ALL_DOWNSTREAM,
        auto_advance: bool = True,
    ) -> dict[str, Any]:
        plan = self._build_plan(workflow_id, scope=scope)
        operation = self._create_operation(plan)
        return await self._run(
            operation["id"],
            advance_current=bool(auto_advance),
        )

    async def resume(self, operation_id: str) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        if operation["status"] == "ABORTED":
            raise ValueError("aborted rebuild operation cannot be resumed")
        if operation["status"] == "COMPLETED":
            return operation
        plan = operation["plan"]
        index = int(operation["current_index"])
        nodes = plan.get("nodes") or []
        if 0 <= index < len(nodes):
            node = nodes[index]
            child_id = str(node.get("new_workflow_id") or "").strip()
            if child_id:
                current = self.workflows.get(child_id)
                if is_recoverable_block(str(current["status"])):
                    # A blocked attempt is a finished execution attempt, not a
                    # mutable version. Resume creates a fresh node in the same
                    # rebuild branch with identical frozen prerequisites.
                    self._cancel_workflow_for_operation(
                        operation,
                        child_id,
                        reason="resume blocked rebuild node with a fresh workflow instance",
                    )
                    node.setdefault("restart_history", []).append(
                        {
                            "workflow_id": child_id,
                            "status": str(current["status"]),
                            "restarted_at": utc_now(),
                        }
                    )
                    node["lineage_parent_workflow_id"] = child_id
                    node["current_relation_type"] = "RESTART_OF"
                    node["new_workflow_id"] = None
                    node["new_status"] = None
                    plan["nodes"][index] = node
                    operation = self._persist_operation(
                        operation,
                        status="RUNNING",
                        current_index=index,
                        plan=plan,
                    )
        return await self._run(operation_id, advance_current=True)

    def abort_and_restore(self, operation_id: str) -> dict[str, Any]:
        """Abort an unfinished rebuild and restore its cancelled source checkpoints.

        Rebuild children are immutable lineage records, so they are cancelled rather
        than deleted.  A source is restored only when its cancellation marker proves
        that this exact operation cancelled it.  The whole change is committed in one
        transaction to avoid leaving the project with both source and replacement
        workflows occupying the same active slot.
        """

        with self.db.transaction() as tx:
            row = tx.fetchone(
                "SELECT * FROM workflow_rebuild_operations WHERE id=?",
                (operation_id,),
            )
            if row is None:
                raise KeyError(f"Rebuild operation not found: {operation_id}")
            if row["status"] == "ABORTED":
                return self._decode_operation(row)
            if row["status"] == "COMPLETED":
                raise ValueError("completed rebuild operation cannot be aborted")

            plan = json.loads(row["plan_json"] or "{}")
            nodes = plan.get("nodes") or []
            created_ids = {
                str(workflow_id)
                for node in nodes
                for workflow_id in (
                    [node.get("new_workflow_id")]
                    + [item.get("workflow_id") for item in (node.get("restart_history") or [])]
                )
                if str(workflow_id or "").strip()
            }

            child_rows: dict[str, dict[str, Any]] = {}
            for workflow_id in sorted(created_ids):
                child = tx.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
                if child is None:
                    raise ValueError(f"rebuild child workflow is missing: {workflow_id}")
                if child["status"] == WorkflowStatus.COMPLETED.value:
                    raise ValueError(
                        "rebuild has a completed replacement workflow and cannot be safely "
                        f"aborted: {workflow_id}"
                    )
                child_rows[workflow_id] = child

            sources_to_restore: list[tuple[dict[str, Any], dict[str, Any], str]] = []
            source_ids = {str(node.get("source_workflow_id") or "") for node in nodes}
            for node in nodes:
                source_id = str(node.get("source_workflow_id") or "").strip()
                if not source_id:
                    continue
                source = tx.fetchone("SELECT * FROM workflows WHERE id=?", (source_id,))
                if source is None:
                    raise ValueError(f"rebuild source workflow is missing: {source_id}")
                state = json.loads(source["state_json"] or "{}")
                marker = state.get("cancelled_for_rebuild") or {}
                if marker.get("operation_id") != operation_id:
                    continue
                if source["status"] != WorkflowStatus.CANCELLED.value:
                    raise ValueError(
                        f"rebuild source has a cancellation marker but is not CANCELLED: {source_id}"
                    )
                restore_status = str(node.get("source_status") or "").strip()
                if restore_status in {"", WorkflowStatus.CANCELLED.value, WorkflowStatus.COMPLETED.value}:
                    raise ValueError(
                        f"rebuild source has no recoverable checkpoint status: {source_id}"
                    )
                # Validate the persisted snapshot against the canonical ontology.  A
                # dedicated restore intentionally bypasses the ordinary terminal-state
                # transition rule after proving operation ownership above.
                WorkflowStatus(restore_status)
                sources_to_restore.append((source, state, restore_status))

            if not sources_to_restore:
                raise ValueError(
                    "rebuild operation has no source checkpoint cancelled by this operation"
                )

            active_conflicts: list[str] = []
            restored_types = {str(source["workflow_type"]) for source, _, _ in sources_to_restore}
            project_rows = tx.fetchall(
                "SELECT id,workflow_type,status FROM workflows WHERE project_id=?",
                (row["project_id"],),
            )
            for workflow in project_rows:
                workflow_id = str(workflow["id"])
                if (
                    workflow_id not in source_ids
                    and workflow_id not in created_ids
                    and str(workflow["workflow_type"]) in restored_types
                    and occupies_workflow_slot(str(workflow["status"]))
                ):
                    active_conflicts.append(
                        f"{workflow['workflow_type']}:{workflow_id}({workflow['status']})"
                    )
            if active_conflicts:
                raise ValueError(
                    "cannot restore source checkpoint while unrelated active workflows "
                    "occupy the same slot: " + ", ".join(sorted(active_conflicts))
                )

            now = utc_now()
            cancelled_children: list[str] = []
            for workflow_id, child in child_rows.items():
                if child["status"] == WorkflowStatus.CANCELLED.value:
                    continue
                ensure_transition(child["status"], WorkflowStatus.CANCELLED.value)
                child_state = json.loads(child["state_json"] or "{}")
                child_state["cancelled_for_rebuild"] = {
                    "cancelled_at": now,
                    "operation_id": operation_id,
                    "branch_id": row["branch_id"],
                    "reason": "rebuild aborted and source checkpoint restored",
                }
                tx.execute(
                    """UPDATE gates
                          SET status='CANCELLED',decision_json=?,updated_at=?
                        WHERE workflow_id=? AND status='OPEN'""",
                    (
                        json.dumps(
                            {
                                "action": "CANCEL",
                                "reason": "rebuild aborted and source checkpoint restored",
                                "rebuild_operation_id": operation_id,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                        workflow_id,
                    ),
                )
                tx.update_workflow(
                    workflow_id=workflow_id,
                    status=WorkflowStatus.CANCELLED.value,
                    current_step=int(child["current_step"]),
                    state=child_state,
                    expected_updated_at=child["updated_at"],
                )
                cancelled_children.append(workflow_id)

            restored_sources: list[str] = []
            for source, state, restore_status in sources_to_restore:
                marker = copy.deepcopy(state.pop("cancelled_for_rebuild"))
                state.setdefault("rebuild_restore_history", []).append(
                    {
                        "restored_at": now,
                        "operation_id": operation_id,
                        "branch_id": row["branch_id"],
                        "restored_status": restore_status,
                        "cancellation": marker,
                    }
                )
                if restore_status == WorkflowStatus.WAITING_GATE.value:
                    cancelled_gates = tx.fetchall(
                        """SELECT id,decision_json FROM gates
                             WHERE workflow_id=? AND status='CANCELLED'""",
                        (source["id"],),
                    )
                    for gate in cancelled_gates:
                        decision = json.loads(gate.get("decision_json") or "{}")
                        if decision.get("rebuild_operation_id") == operation_id:
                            tx.execute(
                                "UPDATE gates SET status='OPEN',decision_json=NULL,updated_at=? WHERE id=?",
                                (now, gate["id"]),
                            )
                tx.update_workflow(
                    workflow_id=str(source["id"]),
                    status=restore_status,
                    current_step=int(source["current_step"]),
                    state=state,
                    expected_updated_at=source["updated_at"],
                )
                restored_sources.append(str(source["id"]))

            plan["abort"] = {
                "aborted_at": now,
                "reason": "restore source workflow checkpoint",
                "restored_source_workflow_ids": restored_sources,
                "cancelled_child_workflow_ids": cancelled_children,
            }
            tx.execute(
                """UPDATE workflow_rebuild_operations
                      SET status='ABORTED',plan_json=?,updated_at=?
                    WHERE id=?""",
                (json.dumps(plan, ensure_ascii=False), now, operation_id),
            )
            tx.audit(
                "WORKFLOW_REBUILD_ABORTED_AND_SOURCE_RESTORED",
                project_id=row["project_id"],
                object_id=operation_id,
                metadata={
                    "branch_id": row["branch_id"],
                    "restored_source_workflow_ids": restored_sources,
                    "cancelled_child_workflow_ids": cancelled_children,
                },
            )

        return self.get_operation(operation_id)

    async def _run(self, operation_id: str, *, advance_current: bool) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        if operation["status"] == "COMPLETED":
            return operation
        plan = operation["plan"]
        nodes = plan.get("nodes") or []
        index = int(operation["current_index"])
        operation = self._persist_operation(
            operation,
            status="RUNNING",
            current_index=index,
            plan=plan,
        )

        while index < len(nodes):
            node = nodes[index]
            child_id = str(node.get("new_workflow_id") or "").strip()
            if not child_id:
                recovered = self._find_started_child(
                    operation["id"], str(node["source_workflow_id"])
                )
                if recovered:
                    child_id = recovered
                    node["new_workflow_id"] = child_id
                else:
                    if node["relation_type"] == "RESTART_OF":
                        self._cancel_source_for_restart(operation, node)
                        node["cancelled_source"] = True
                    explicit_bindings = self._remap_bindings(plan, node)
                    options = copy.deepcopy(node.get("options") or {})
                    options["idempotency_key"] = (
                        f"lineage-{operation['id']}-{int(node['position']):03d}"
                        f"-attempt-{len(node.get('restart_history') or []):03d}"
                    )
                    created = self.workflows.start(
                        operation["project_id"],
                        node["workflow_type"],
                        options,
                        prerequisite_workflow_ids=explicit_bindings,
                        lifecycle_context={
                            "rebuild_operation_id": operation["id"],
                            "branch_id": operation["branch_id"],
                            "source_workflow_id": node["source_workflow_id"],
                            "relation_type": node["relation_type"],
                        },
                    )
                    child_id = str(created["id"])
                    node["new_workflow_id"] = child_id
                    node["new_status"] = str(created["status"])
                self._record_lineage(operation, node, child_id)
                operation = self._persist_operation(
                    operation,
                    status="RUNNING",
                    current_index=index,
                    plan=plan,
                )

            current = self.workflows.get(child_id)
            if current["status"] != WorkflowStatus.COMPLETED.value and advance_current:
                current = await self.workflows.advance(child_id)
            node["new_status"] = str(current["status"])
            plan["nodes"][index] = node
            if current["status"] != WorkflowStatus.COMPLETED.value:
                operation = self._persist_operation(
                    operation,
                    status="PAUSED",
                    current_index=index,
                    plan=plan,
                )
                self.db.audit(
                    "WORKFLOW_REBUILD_PAUSED",
                    project_id=operation["project_id"],
                    object_id=operation["id"],
                    metadata={
                        "active_workflow_id": child_id,
                        "active_workflow_type": node["workflow_type"],
                        "workflow_status": current["status"],
                    },
                )
                return operation

            index += 1
            operation = self._persist_operation(
                operation,
                status="RUNNING" if index < len(nodes) else "COMPLETED",
                current_index=index,
                plan=plan,
            )

        self.db.audit(
            "WORKFLOW_REBUILD_COMPLETED",
            project_id=operation["project_id"],
            object_id=operation["id"],
            metadata={
                "branch_id": operation["branch_id"],
                "created_workflow_ids": operation["created_workflow_ids"],
            },
        )
        return self.get_operation(operation["id"])
