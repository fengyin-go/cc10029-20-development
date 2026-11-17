"""代理池接口：代理节点维护，外加存活检测批次、可用代理数据集与代理分配记录。

既有接口（``GET /api/proxy``、``/export``、``/{entry_id}``、动作接口）保持原样；
新增接口统一挂在 ``/available``、``/batches``、``/detect``、``/assignments`` 下。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.proxy import AVAILABLE_FIELDS, ASSIGNMENT_FIELDS, ProxyService

router = APIRouter(prefix="/api/proxy", tags=["代理池"])

service = ProxyService()

LIST_FIELDS = ["节点编号", "IP地址", "端口号", "协议类型", "匿程度", "响应延迟", "存活检测", "所属区域"]
AVAILABLE_FILTER_FIELDS = ["协议类型", "所属区域", "区域标签", "可用性", "检测结果", "节点编号", "IP地址"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def available_filters(request: Request) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in AVAILABLE_FILTER_FIELDS and value.strip()
    }


def csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def build_csv(header: list[str], rows: list[dict[str, Any]]) -> StreamingResponse:
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    for item in rows:
        writer.writerow([csv_cell(item.get(field, "")) for field in header])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="proxy-available.csv"'})


# ---------------------------------------------------------------------- 可用代理数据集
@router.get("/available/summary", response_model=dict)
def available_summary() -> dict:
    """可用代理数据集的批次绑定、可分配数量与冲突统计。"""
    return service.available_summary()


@router.get("/available/export")
def export_available(
    request: Request,
    keyword: str | None = Query(default=None),
    max_latency: int | None = Query(default=None, ge=0, description="只看响应延迟不超过该值的节点"),
    sort: str = Query(default="排序序号"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> StreamingResponse:
    """导出当前筛选下的可用代理集合；按节点编号去重，保证导出里每个节点只出现一次。"""
    rows, _, _ = service.list_available(
        filters=available_filters(request), keyword=keyword, max_latency=max_latency,
        sort=sort, order=order, page=1, size=1000000)
    unique = service.export_available(rows)
    header = ["id", *AVAILABLE_FIELDS, "不可用原因"]
    return build_csv(header, unique)


@router.get("/available", response_model=PageResult[dict])
def list_available(
    request: Request,
    keyword: str | None = Query(default=None, description="按节点编号或 IP 检索"),
    max_latency: int | None = Query(default=None, ge=0, description="响应延迟上限（毫秒）"),
    sort: str = Query(default="排序序号", description="排序字段"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """读取可用代理数据集：支持按协议、区域、检测结果和延迟上限筛选，排序顺序持久化。"""
    rows, total, summary = service.list_available(
        filters=available_filters(request), keyword=keyword, max_latency=max_latency,
        sort=sort, order=order, page=page, size=size)
    return PageResult(items=rows, total=total, page=page, size=size, summary=summary)


@router.post("/available/reorder", response_model=ActionResult)
def reorder_available(payload: dict[str, Any]) -> ActionResult:
    """按用户给出的记录顺序保存排序；刷新后仍按最新顺序返回。"""
    ordered = payload.get("顺序") or payload.get("ids") or []
    if not isinstance(ordered, list) or not ordered:
        return ActionResult(ok=False, message="请提交非空的顺序列表")
    try:
        ids = [int(item) for item in ordered]
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="顺序列表里只能是记录编号")
    rows, message = service.reorder_available(ids)
    if not rows:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message)


@router.post("/available", response_model=ActionResult)
def submit_available(payload: dict[str, Any]) -> ActionResult:
    """送交一份可用代理数据集；同一集合编号重复送交只保留一个编号。"""
    entry, message = service.submit_available(payload)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/available/{entry_id}", response_model=dict)
def get_available(entry_id: int) -> dict:
    entry = service.get_available(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"可用代理记录 {entry_id} 不存在")
    return entry


# ---------------------------------------------------------------------- 存活检测
@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """存活检测批次列表，最近一次批次会被标记出来。"""
    rows = service.list_batches()
    start = (page - 1) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/detect", response_model=ActionResult)
def run_detection(payload: dict[str, Any]) -> ActionResult:
    """执行一轮存活检测：写新批次并把可用代理数据集重建到该批次上。"""
    entry, message = service.run_detection(payload)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ---------------------------------------------------------------------- 代理分配记录
@router.get("/assignments", response_model=PageResult[dict])
def list_assignments(
    task_no: str | None = Query(default=None, description="按任务编号过滤"),
    collection_no: str | None = Query(default=None, description="按可用代理集合编号过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    rows, total = service.list_assignments(task_no=task_no, collection_no=collection_no, page=page, size=size)
    return PageResult(items=rows, total=total, page=page, size=size)


@router.get("/assignments/export")
def export_assignments(
    task_no: str | None = Query(default=None),
    collection_no: str | None = Query(default=None),
) -> StreamingResponse:
    """导出代理分配记录台账。"""
    rows, _ = service.list_assignments(
        task_no=task_no, collection_no=collection_no, page=1, size=1000000)
    return build_csv(["id", *ASSIGNMENT_FIELDS], rows)


@router.post("/assignments", response_model=ActionResult)
def generate_assignments(payload: dict[str, Any]) -> ActionResult:
    """爬虫引擎消费当前可用代理数据集生成分配记录；只引用当前可用节点与检测结果。"""
    rows, message = service.generate_assignments(payload)
    if rows is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True, message=message,
        entry={"记录数": len(rows), "集合编号": rows[0].get("集合编号") if rows else "", "items": rows})


# ---------------------------------------------------------------------- 既有代理节点接口
@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、节点编号关键字与状态过滤代理池列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="proxy.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条代理节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"代理节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条代理节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="代理节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条代理节点执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
