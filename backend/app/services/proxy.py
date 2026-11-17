"""代理池业务规则：状态流转、存活检测批次、可用代理数据集与代理分配记录。

可用性口径（四个条件同时成立才算可分配）：
1. 响应延迟可解析且不超过 ``LATENCY_LIMIT_MS``；
2. 协议类型在支持的协议集合内；
3. 所属区域非空且在可分发区域集合内；
4. 节点必须在本池最近一次存活检测批次里被检测过，且批次没有过期。

可用代理数据集绑定最近一次存活检测批次；同一节点只保留一条最新记录（重复检测
只更新状态）。写操作统一走 ``_commit``：失败时整批回滚，保留上一版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.store import store

MODULE = "proxy"
LIST_FIELDS = ["节点编号", "IP地址", "端口号", "协议类型", "匿程度", "响应延迟", "存活检测", "所属区域"]
REQUIRED_FIELDS = ["节点编号", "IP地址", "端口号"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

BATCH_TABLE = "proxy_detection_batches"
DATASET_TABLE = "proxy_available_sets"
ASSIGNMENT_TABLE = "proxy_assignments"
TRANSACTION_TABLES = (MODULE, BATCH_TABLE, DATASET_TABLE, ASSIGNMENT_TABLE)

SUPPORTED_PROTOCOLS = ["HTTP", "HTTPS", "SOCKS5"]
SUPPORTED_REGIONS = ["华北", "华东", "华南", "西南", "西北", "海外"]
LATENCY_LIMIT_MS = 500
DEFAULT_EXPIRY_MINUTES = 720
DATASET_STATUS = "当前"

AVAILABLE_FIELDS = [
    "集合编号", "批次编号", "检测时间", "节点编号", "IP地址", "端口号", "协议类型",
    "所属区域", "区域标签", "响应延迟", "响应延迟展示", "检测结果", "检测依据",
    "可用性", "可分配", "冲突标记", "冲突说明", "排序序号",
]

ASSIGNMENT_FIELDS = [
    "分配编号", "集合编号", "批次编号", "任务编号", "节点编号", "协议类型",
    "所属区域", "响应延迟", "检测时间", "检测依据", "分配状态", "创建时间",
]


class ProxyTransactionError(RuntimeError):
    """事务中途失败：由 ``_commit`` 捕获并整批回滚。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _parse_moment(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("/", "-").replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def parse_latency(value: Any) -> int | None:
    """把 ``240ms`` / ``240`` / ``0.24s`` 统一成毫秒整数；解析不了返回 None。"""
    if value is None:
        return None
    text = str(value).strip().lower().replace("毫秒", "").replace("ms", "")
    scale = 1
    if text.endswith("s"):
        text = text[:-1].strip()
        scale = 1000
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0:
        return None
    return int(round(number * scale))


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_code(prefix: str, rows: list[dict[str, Any]], field: str) -> str:
    numbers = []
    for row in rows:
        text = str(row.get(field, ""))
        if text.startswith(prefix + "-"):
            try:
                numbers.append(int(text.split("-")[-1]))
            except ValueError:
                continue
    return f"{prefix}-{max(numbers, default=0) + 1:04d}"


class ProxyService:
    # ------------------------------------------------------------------ 既有接口
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("节点编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        if values.get("区域标签") is not None:
            entry["区域标签"] = values.get("区域标签")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"代理节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于代理池可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"代理节点已{action}"

    # ------------------------------------------------------------------ 事务
    def _commit(self, mutate: Callable[[], Any]) -> tuple[Any, str]:
        """执行写操作；任何异常都还原整批表并保留上一版本。"""
        snapshot = store.snapshot(TRANSACTION_TABLES)
        try:
            return mutate(), ""
        except ProxyTransactionError as exc:
            store.restore(snapshot)
            return None, f"事务提交失败，已回滚到上一版本：{exc}"
        except Exception as exc:  # noqa: BLE001 - 回滚后要把原因交回调用方
            store.restore(snapshot)
            return None, f"事务提交失败，已回滚到上一版本：{exc}"

    # ------------------------------------------------------------------ 批次
    def latest_batch(self) -> dict[str, Any] | None:
        rows = store.rows(BATCH_TABLE)
        flagged = [row for row in rows if row.get("当前批次")]
        if flagged:
            return max(flagged, key=lambda row: (str(row.get("检测时间", "")), int(row.get("id", 0))))
        if not rows:
            return None
        return max(rows, key=lambda row: (str(row.get("检测时间", "")), int(row.get("id", 0))))

    def batch_expired(self, batch: dict[str, Any] | None) -> bool:
        if batch is None:
            return True
        checked = _parse_moment(batch.get("检测时间"))
        if checked is None:
            return True
        try:
            expiry = int(batch.get("过期分钟") or DEFAULT_EXPIRY_MINUTES)
        except (TypeError, ValueError):
            expiry = DEFAULT_EXPIRY_MINUTES
        age_minutes = (datetime.now() - checked).total_seconds() / 60
        return age_minutes > expiry

    def list_batches(self) -> list[dict[str, Any]]:
        latest = self.latest_batch()
        latest_id = int(latest.get("id", 0)) if latest else 0
        rows = sorted(
            store.rows(BATCH_TABLE),
            key=lambda row: (str(row.get("检测时间", "")), int(row.get("id", 0))),
            reverse=True,
        )
        return [
            {
                **row,
                "是否最近批次": int(row.get("id", 0)) == latest_id,
                "检测是否过期": self.batch_expired(row) if int(row.get("id", 0)) == latest_id else True,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ 可用性判定
    def _evaluate_node(self, node: dict[str, Any], batch: dict[str, Any], expired: bool) -> dict[str, Any]:
        """按协议、延迟、区域与批次检测结果判定单个节点；返回一份带检测依据的结论。"""
        reasons: list[str] = []
        protocol = str(node.get("协议类型") or "").strip().upper()
        if protocol not in SUPPORTED_PROTOCOLS:
            reasons.append(f"协议类型「{node.get('协议类型') or '空'}」不支持")
        latency = parse_latency(node.get("响应延迟"))
        if latency is None:
            reasons.append("响应延迟无法解析")
        elif latency > LATENCY_LIMIT_MS:
            reasons.append(f"响应延迟 {latency}ms 超过阈值 {LATENCY_LIMIT_MS}ms")
        detected_region = str(node.get("所属区域") or "").strip()
        if detected_region not in SUPPORTED_REGIONS:
            reasons.append(f"所属区域「{detected_region or '空'}」不在可分发范围")
        if str(node.get("status")) == STATUS_ORDER[-1]:
            reasons.append("节点已停用")

        checked = str(node.get("存活检测") or "").strip()
        batch_time = str(batch.get("检测时间") or "").strip()
        if str(node.get("abnormal")).lower() in ("true", "1") and checked == batch_time:
            reasons.append("最近批次检测判定节点不可用")
        checked_at = _parse_moment(checked)
        batch_at = _parse_moment(batch_time)
        in_batch = checked_at is not None and batch_at is not None and checked_at >= batch_at
        if not in_batch:
            reasons.append(f"存活检测 {checked or '空'} 早于最近批次 {batch_time or '空'}，检测已过期")
        if expired:
            reasons.append(f"检测批次 {batch.get('批次编号') or ''} 已过期")

        tag = str(node.get("区域标签") or "").strip()
        conflict = bool(tag) and tag != detected_region
        if conflict:
            # 冲突只保留一份记录：以检测结果为准，检测依据里保留标签与实测区域的差异。
            basis = (
                f"{batch.get('批次编号', '')} 于 {batch_time} 探活，响应 {latency if latency is not None else '未知'}ms；"
                f"区域标签「{tag}」与检测区域「{detected_region}」不一致，按检测结果分配"
            )
        else:
            basis = (
                f"{batch.get('批次编号', '')} 于 {batch_time} 探活，响应 "
                f"{latency if latency is not None else '未知'}ms，区域 {detected_region or '未知'}"
            )

        available = not reasons
        return {
            "协议类型": protocol or str(node.get("协议类型") or ""),
            "所属区域": detected_region,
            "区域标签": tag or detected_region,
            "响应延迟": latency if latency is not None else 0,
            "响应延迟展示": f"{latency}ms" if latency is not None else "—",
            "检测结果": (
                "检测过期" if not in_batch or expired
                else "存活" if str(node.get("abnormal")).lower() not in ("true", "1") else "不可用"
            ),
            "检测依据": basis,
            "可用性": "可用" if available else "不可用",
            "可分配": available,
            "不可用原因": "；".join(reasons),
            "冲突标记": conflict,
            "冲突说明": (
                f"区域标签「{tag}」与检测区域「{detected_region}」冲突，已按检测结果保留一条记录"
                if conflict else ""
            ),
        }

    def _build_dataset_rows(
        self,
        *,
        batch: dict[str, Any],
        collection_no: str,
        nodes: list[dict[str, Any]],
        order_start: int = 1,
    ) -> list[dict[str, Any]]:
        """为一个批次生成可用代理记录：同节点只留一条，区域冲突合并成一条。"""
        expired = self.batch_expired(batch)
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for node in nodes:
            node_no = str(node.get("节点编号") or "").strip()
            if not node_no or node_no in seen:
                continue
            seen.add(node_no)
            evaluated = self._evaluate_node(node, batch, expired)
            rows.append({
                "id": 0,
                "集合编号": collection_no,
                "批次编号": str(batch.get("批次编号") or ""),
                "检测时间": str(batch.get("检测时间") or ""),
                "节点编号": node_no,
                "IP地址": node.get("IP地址"),
                "端口号": node.get("端口号"),
                "排序序号": order_start + len(rows),
                **evaluated,
            })
        return rows

    def _apply_dataset(self, rows: list[dict[str, Any]]) -> None:
        table = store.rows(DATASET_TABLE)
        table.clear()
        for index, row in enumerate(rows, start=1):
            row["id"] = index
            row.setdefault("状态", DATASET_STATUS)
            table.append(row)

    # ------------------------------------------------------------------ 可用代理数据集
    def available_summary(self) -> dict[str, Any]:
        batch = self.latest_batch()
        rows = store.rows(DATASET_TABLE)
        return {
            "批次编号": batch.get("批次编号") if batch else "",
            "检测时间": batch.get("检测时间") if batch else "",
            "检测是否过期": self.batch_expired(batch),
            "集合编号": rows[0].get("集合编号") if rows else "",
            "记录数": len(rows),
            "可分配节点数": sum(1 for row in rows if row.get("可分配")),
            "冲突记录数": sum(1 for row in rows if row.get("冲突标记")),
        }

    def list_available(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        max_latency: int | None = None,
        sort: str = "排序序号",
        order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        rows = list(store.rows(DATASET_TABLE))
        batch = self.latest_batch()
        expired = self.batch_expired(batch)
        for field, expected in (filters or {}).items():
            if field not in AVAILABLE_FIELDS and field not in ("节点编号", "IP地址"):
                continue
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("节点编号", "")).casefold()
                or needle in str(row.get("IP地址", "")).casefold()
            ]
        if max_latency is not None:
            rows = [row for row in rows if int(row.get("响应延迟") or 0) <= max_latency]
        if expired:
            rows = [
                {**row, "可用性": "暂不可分配", "可分配": False,
                 "不可用原因": "最近一次存活检测批次已过期，需重新检测"}
                for row in rows
            ]

        sort_key = sort if sort in AVAILABLE_FIELDS else "排序序号"
        reverse = order == "desc"

        def sort_value(row: dict[str, Any]) -> Any:
            value = row.get(sort_key)
            if sort_key in ("响应延迟", "排序序号"):
                try:
                    return int(value or 0)
                except (TypeError, ValueError):
                    return 0
            if sort_key == "可分配":
                return bool(value)
            return str(value or "")

        # 主排序键生效，其余同值记录按用户保存的排序序号排定，保证顺序可复现。
        rows.sort(key=lambda row: int(row.get("排序序号") or 0))
        rows.sort(key=sort_value, reverse=reverse)

        total = len(rows)
        start = (page - 1) * size
        summary = self.available_summary()
        summary["检测是否过期"] = expired
        return rows[start:start + size], total, summary

    def get_available(self, entry_id: int) -> dict[str, Any] | None:
        rows = store.rows(DATASET_TABLE)
        for position, row in enumerate(rows, start=1):
            if int(row.get("id", 0)) == entry_id:
                return {**row, "位次": position}
        return None

    def export_available(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """导出前按节点编号去重，保证一份节点只出现一次。"""
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            node_no = str(row.get("节点编号") or "")
            if node_no in seen:
                continue
            seen.add(node_no)
            unique.append(row)
        return unique

    def submit_available(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """送交一份可用代理数据集；同一集合编号重复送交只返回已存在的那一份。"""
        collection_no = str(payload.get("集合编号") or "").strip()
        table = store.rows(DATASET_TABLE)
        if collection_no:
            existing = [row for row in table if str(row.get("集合编号")) == collection_no]
            if existing:
                return (
                    {"集合编号": collection_no, "记录数": len(existing), "id": existing[0].get("id")},
                    f"可用代理数据集 {collection_no} 已存在，仅保留一个编号",
                )

        batch = self._resolve_batch(payload)
        nodes = self._resolve_nodes(payload)
        if batch is None:
            return None, "没有可用的存活检测批次，请先执行存活检测"
        latest = self.latest_batch()
        if latest is None or str(batch.get("批次编号")) != str(latest.get("批次编号")):
            return None, (
                f"可用代理数据集必须绑定最近一次存活检测批次 "
                f"{latest.get('批次编号') if latest else '（无）'}，不能引用历史批次"
            )
        if self.batch_expired(batch):
            return None, f"最近一次存活检测批次 {batch.get('批次编号')} 已过期，不能生成可用代理数据集"
        if not nodes:
            return None, "没有可纳入的代理节点"
        if not collection_no:
            collection_no = _next_code("PSET", table, "集合编号")

        def mutate() -> dict[str, Any]:
            rows = self._build_dataset_rows(batch=batch, collection_no=collection_no, nodes=nodes)
            if payload.get("simulate_failure"):
                raise ProxyTransactionError("模拟提交失败")
            if not rows:
                raise ProxyTransactionError("可用代理数据集为空")
            self._apply_dataset(rows)
            self._reindex_assignments(batch)
            return {
                "集合编号": collection_no,
                "id": rows[0]["id"],
                "记录数": len(rows),
                "可分配节点数": sum(1 for row in rows if row.get("可分配")),
            }

        result, error = self._commit(mutate)
        if error:
            return None, error
        return result, f"可用代理数据集 {collection_no} 已生成，共 {result['记录数']} 条"

    def reorder_available(self, ordered_ids: list[int]) -> tuple[list[dict[str, Any]], str]:
        """按用户给出的顺序重排并持久化，刷新后仍按最新顺序返回。"""
        rows = store.rows(DATASET_TABLE)
        positions = {int(row.get("id", 0)): row for row in rows}
        unknown = [str(entry_id) for entry_id in ordered_ids if int(entry_id) not in positions]
        if unknown:
            return [], f"排序失败：记录 {'、'.join(unknown)} 不在当前可用代理数据集里"
        if len(set(int(entry_id) for entry_id in ordered_ids)) != len(ordered_ids):
            return [], "排序失败：同一记录不能重复出现"

        def mutate() -> list[dict[str, Any]]:
            next_order = 1
            for entry_id in ordered_ids:
                positions[int(entry_id)]["排序序号"] = next_order
                next_order += 1
            # 未出现在本次顺序里的记录顺延到末尾，保持集合完整。
            for row in rows:
                if int(row.get("id", 0)) not in {int(entry_id) for entry_id in ordered_ids}:
                    row["排序序号"] = next_order
                    next_order += 1
            rows.sort(key=lambda row: int(row.get("排序序号") or 0))
            return rows

        result, error = self._commit(mutate)
        if error:
            return [], error
        return result or [], "可用代理数据集顺序已保存"

    # ------------------------------------------------------------------ 存活检测
    def run_detection(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """执行一次存活检测：更新节点最新状态、写新批次、重建可用代理数据集。"""
        nodes = store.rows(MODULE)
        if not nodes:
            return None, "代理池没有可检测的节点"
        batch_time = str(payload.get("检测时间") or _now()).strip()
        if _parse_moment(batch_time) is None:
            return None, f"检测时间「{batch_time}」格式无法识别"
        try:
            expiry = int(payload.get("过期分钟") or DEFAULT_EXPIRY_MINUTES)
        except (TypeError, ValueError):
            return None, "过期分钟必须是整数"
        if expiry <= 0:
            return None, "过期分钟必须大于 0"

        detections = payload.get("检测结果") or payload.get("nodes") or []
        if not isinstance(detections, list):
            return None, "检测结果必须是节点检测明细列表"
        by_node: dict[str, dict[str, Any]] = {}
        for item in detections:
            if not isinstance(item, dict):
                return None, "检测结果里的每条记录都必须是对象"
            node_no = str(item.get("节点编号") or "").strip()
            if not node_no:
                return None, "检测结果缺少节点编号"
            by_node[node_no] = item
        known = {str(node.get("节点编号")) for node in nodes}
        unknown = [node_no for node_no in by_node if node_no not in known]
        if unknown:
            return None, f"检测结果引用了不存在的节点：{'、'.join(unknown)}"

        def mutate() -> dict[str, Any]:
            alive = 0
            for node in nodes:
                node_no = str(node.get("节点编号"))
                update = by_node.get(node_no)
                if update is None:
                    if not by_node:
                        # 只给时间、不给明细时按全量探活处理。
                        node["存活检测"] = batch_time
                    continue
                node["存活检测"] = batch_time
                if "存活" in update and not update.get("存活"):
                    node["abnormal"] = True
                else:
                    node["abnormal"] = False
                if update.get("响应延迟") is not None:
                    node["响应延迟"] = update.get("响应延迟")
                if update.get("协议类型") is not None:
                    node["协议类型"] = update.get("协议类型")
                if update.get("所属区域") is not None:
                    node["所属区域"] = update.get("所属区域")
                if update.get("区域标签") is not None:
                    node["区域标签"] = update.get("区域标签")

            # 未显式给出检测结果的节点，视为本轮未检测：保留旧时间，从而不进入新批次。
            checked_nodes = [
                node for node in nodes
                if not by_node or str(node.get("节点编号")) in by_node
            ]
            alive = sum(
                1 for node in checked_nodes
                if not node.get("abnormal") and str(node.get("存活检测")) == batch_time
            )

            batches = store.rows(BATCH_TABLE)
            for row in batches:
                row["当前批次"] = False
            batch_id = _next_id(batches)
            batch = {
                "id": batch_id,
                "批次编号": _next_code("PBAT", batches, "批次编号"),
                "检测时间": batch_time,
                "过期分钟": expiry,
                "状态": "已完成",
                "检测节点数": len(checked_nodes),
                "存活节点数": alive,
                "当前批次": True,
            }
            batches.append(batch)

            dataset_rows = self._build_dataset_rows(
                batch=batch, collection_no=_next_collection_no(), nodes=checked_nodes)
            if payload.get("simulate_failure"):
                raise ProxyTransactionError("模拟提交失败")
            if not dataset_rows:
                raise ProxyTransactionError("检测后没有生成任何可用代理记录")
            self._apply_dataset(dataset_rows)
            self._reindex_assignments(batch)
            return {
                "批次编号": batch["批次编号"],
                "检测时间": batch_time,
                "过期分钟": expiry,
                "检测节点数": batch["检测节点数"],
                "存活节点数": alive,
                "集合编号": dataset_rows[0].get("集合编号"),
                "记录数": len(dataset_rows),
                "可分配节点数": sum(1 for row in dataset_rows if row.get("可分配")),
            }

        result, error = self._commit(mutate)
        if error:
            return None, error
        return result, (
            f"存活检测批次 {result['批次编号']} 已完成，可用代理数据集 "
            f"{result['集合编号']} 共 {result['记录数']} 条"
        )

    # ------------------------------------------------------------------ 代理分配
    def list_assignments(
        self,
        *,
        task_no: str | None = None,
        collection_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(ASSIGNMENT_TABLE))
        if task_no:
            rows = [row for row in rows if str(row.get("任务编号")) == task_no]
        if collection_no:
            rows = [row for row in rows if str(row.get("集合编号")) == collection_no]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def generate_assignments(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str]:
        """爬虫引擎消费可用代理数据集，生成只引用当前可用节点与检测结果的分配记录。"""
        task_no = str(payload.get("任务编号") or "").strip()
        if not task_no:
            return None, "缺少任务编号，无法生成代理分配记录"
        task = self._find_crawler_task(task_no)
        if task is None:
            return None, f"爬虫任务 {task_no} 不存在，无法生成代理分配记录"

        batch = self.latest_batch()
        if batch is None:
            return None, "没有存活检测批次，无法生成代理分配记录"
        if self.batch_expired(batch):
            return None, f"最近一次存活检测批次 {batch.get('批次编号')} 已过期，不能继续分配"

        rows = store.rows(DATASET_TABLE)
        collection_no = str(payload.get("集合编号") or (rows[0].get("集合编号") if rows else "")).strip()
        if not collection_no:
            return None, "没有可用代理数据集，无法生成代理分配记录"
        dataset = [row for row in rows if str(row.get("集合编号")) == collection_no]
        if not dataset:
            return None, f"可用代理数据集 {collection_no} 不存在"
        if any(str(row.get("批次编号")) != str(batch.get("批次编号")) for row in dataset):
            return None, "可用代理数据集未绑定最近一次存活检测批次，不能分配"

        requested = payload.get("节点编号列表") or payload.get("节点编号") or []
        if isinstance(requested, str):
            requested = [requested]
        requested = [str(item).strip() for item in requested if str(item).strip()]
        candidates = [row for row in dataset if row.get("可分配")]
        if requested:
            available_nodes = {str(row.get("节点编号")) for row in candidates}
            missing = [node_no for node_no in requested if node_no not in available_nodes]
            if missing:
                return None, (
                    f"节点 {'、'.join(missing)} 不在当前可用代理数据集内或已不可分配，"
                    "分配记录只能引用当前可用节点"
                )
            candidates = [row for row in candidates if str(row.get("节点编号")) in requested]

        try:
            limit = int(payload.get("数量") or 0)
        except (TypeError, ValueError):
            return None, "数量必须是整数"
        if limit > 0:
            candidates = candidates[:limit]
        if not candidates:
            return None, "当前可用代理数据集里没有可分配节点"

        existing = [
            row for row in store.rows(ASSIGNMENT_TABLE)
            if str(row.get("集合编号")) == collection_no and str(row.get("任务编号")) == task_no
            and str(row.get("分配状态")) == "已分配"
        ]
        if existing:
            return existing, f"任务 {task_no} 已按数据集 {collection_no} 生成过分配记录，未重复创建"

        def mutate() -> list[dict[str, Any]]:
            table = store.rows(ASSIGNMENT_TABLE)
            created: list[dict[str, Any]] = []
            for item in candidates:
                record = {
                    "id": 0,
                    "分配编号": "",
                    "集合编号": collection_no,
                    "批次编号": item.get("批次编号"),
                    "任务编号": task_no,
                    "节点编号": item.get("节点编号"),
                    "协议类型": item.get("协议类型"),
                    "所属区域": item.get("所属区域"),
                    "响应延迟": item.get("响应延迟"),
                    "检测时间": item.get("检测时间"),
                    "检测依据": item.get("检测依据"),
                    "分配状态": "已分配",
                    "创建时间": _now(),
                }
                table.append(record)
                created.append(record)
            if payload.get("simulate_failure"):
                raise ProxyTransactionError("模拟提交失败")
            for record in created:
                record["id"] = _next_id(table)
                record["分配编号"] = _next_code("PASG", table, "分配编号")
            return created

        created, error = self._commit(mutate)
        if error:
            return None, error
        return created or [], f"已生成 {len(created or [])} 条代理分配记录"

    # ------------------------------------------------------------------ 内部辅助
    def _reindex_assignments(self, batch: dict[str, Any]) -> None:
        """数据集换版后，把分配记录同步到最新可用集合：节点失效或已删除的记录改为失效。"""
        dataset = store.rows(DATASET_TABLE)
        collection_no = str(dataset[0].get("集合编号")) if dataset else ""
        available = {
            str(row.get("节点编号")): row
            for row in dataset
        }
        for record in store.rows(ASSIGNMENT_TABLE):
            node_no = str(record.get("节点编号"))
            target = available.get(node_no)
            if target is None:
                record["分配状态"] = "已失效"
                continue
            if str(target.get("批次编号")) != str(batch.get("批次编号")):
                record["分配状态"] = "已失效"
                continue
            record.update({
                "集合编号": collection_no,
                "批次编号": target.get("批次编号"),
                "响应延迟": target.get("响应延迟"),
                "检测时间": target.get("检测时间"),
                "检测依据": target.get("检测依据"),
                "分配状态": "已分配" if target.get("可分配") else "已失效",
            })

    def _resolve_batch(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        batch_no = str(payload.get("批次编号") or "").strip()
        if batch_no:
            for row in store.rows(BATCH_TABLE):
                if str(row.get("批次编号")) == batch_no:
                    return row
            return None
        return self.latest_batch()

    def _resolve_nodes(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        requested = payload.get("节点编号列表") or payload.get("节点编号") or []
        if isinstance(requested, str):
            requested = [requested]
        wanted = {str(item).strip() for item in requested if str(item).strip()}
        nodes = store.rows(MODULE)
        if wanted:
            return [node for node in nodes if str(node.get("节点编号")) in wanted]
        return list(nodes)

    def available_by_node(self) -> dict[str, dict[str, Any]]:
        """把当前可用代理数据集按节点编号索引，供爬虫引擎等下游只读消费。"""
        return {str(row.get("节点编号")): row for row in store.rows(DATASET_TABLE)}

    @staticmethod
    def _find_crawler_task(task_no: str) -> dict[str, Any] | None:
        for index, row in enumerate(store.rows("crawler"), start=1):
            if str(row.get("任务编号")) == task_no or str(row.get("id")) == task_no:
                return row
            if task_no.isdigit() and int(row.get("id", 0)) == int(task_no):
                return row
        return None


def _next_collection_no() -> str:
    """集合编号在整张数据集表里单调递增，避免重复送交时撞号。"""
    return _next_code("PSET", store.rows(DATASET_TABLE), "集合编号")
