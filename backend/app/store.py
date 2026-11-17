"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import PROXY_EXTRA_ROWS, SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 代理池的派生集合与主表共用同一个仓库，保证事务回滚时能一起快照 / 还原。
        for name, rows in PROXY_EXTRA_ROWS.items():
            self._tables.setdefault(name, [dict(row) for row in rows])

    def module_names(self) -> list[str]:
        """只暴露 README 里登记的业务模块；代理池的派生集合是内部表，不占概览行。"""
        return sorted(name for name in self._tables if name in SEED_ROWS)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def snapshot(self, modules: list[str] | tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        """给多表事务留一份深拷贝快照；提交失败时用它整批还原。"""
        return {
            name: [dict(row) for row in self.rows(name)]
            for name in modules
        }

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        """把快照原样写回；只替换表内容，保留表对象本身供其他引用继续使用。"""
        for name, rows in snapshot.items():
            self._tables[name] = [dict(row) for row in rows]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
