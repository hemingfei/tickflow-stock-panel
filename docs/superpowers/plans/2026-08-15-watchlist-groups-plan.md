# 自选板块功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现自选板块功能，用户可创建多个自定义板块，每个板块独立管理股票列表，支持多种视图模式，实时行情刷新与原有自选股完全兼容

**Architecture:**
- 后端: 新增 `watchlist_groups.py` service 和 API，复用现有 `watchlist.py` 模式
- 前端: 新增 `WatchlistGroups.tsx` 页面，复用 `Watchlist.tsx` 组件和 API 模式
- 数据存储: Parquet 文件（`watchlist_groups.parquet` 和 `watchlist_group_items.parquet`）
- 实时刷新: 把所有板块股票合并到 `QuoteService` 的订阅池

**Tech Stack:**
- 后端: Python, FastAPI, Polars, Parquet
- 前端: React, TypeScript, TanStack Query, Tailwind CSS

**Spec:** [2026-08-15-watchlist-groups-design.md](../specs/2026-08-15-watchlist-groups-design.md)

---

## Global Constraints

1. **数据口径一致性**: 沿用现有 `watchlist.py` 的 Parquet 读写模式
2. **向后兼容**: 不修改现有 `watchlist.parquet` 和相关 API
3. **复用原则**: 最大程度复用 `api.ts` 类型、`Watchlist.tsx` 组件、`quote_service.py` 等
4. **百分比格式**: `change_pct` 在内部存储为小数，前端显示为百分比（如 0.0235 → 2.35%）
5. **板块名称唯一**: 创建和重命名时必须校验名称唯一性
6. **实时订阅合并**: 所有板块股票与原有自选股合并到订阅池

---

## 文件结构概览

```
backend/app/
├── services/
│   └── watchlist_groups.py      # 新增: 板块 CRUD 服务
└── api/
    └── watchlist_groups.py      # 新增: 板块 API 路由

frontend/src/
├── pages/
│   └── WatchlistGroups.tsx      # 新增: 板块主页面
└── lib/
    └── api.ts                   # 修改: 新增板块相关类型和 API

backend/app/main.py              # 修改: 注册新 API 路由
backend/app/services/preferences.py # 修改: 新增全局设置存取
```

---

## Task 1: 后端板块服务 (watchlist_groups.py)

**Files:**
- Create: `backend/app/services/watchlist_groups.py`

**Interfaces:**
- Produces: `list_groups()`, `create_group()`, `rename_group()`, `delete_group()`, `reorder_groups()`
- Produces: `list_group_items()`, `add_item()`, `remove_item()`, `reorder_items()`
- Produces: `get_all_watchlist_symbols()` (供实时订阅合并使用)

**设计参考:** 完全复用 `watchlist.py` 的 Parquet 读写模式

---

### Task 1a: 实现基础数据结构和文件路径

- [ ] **Step 1: 创建文件并编写文件路径函数**

```python
"""自选板块服务。

存储:
- data/user_data/watchlist_groups.parquet: 板块元数据 (group_id, name, order, created_at)
- data/user_data/watchlist_group_items.parquet: 板块-股票关联 (group_id, symbol, order, added_at)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
import uuid

import polars as pl

logger = logging.getLogger(__name__)


def _groups_path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "watchlist_groups.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _items_path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "watchlist_group_items.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
```

- [ ] **Step 2: 实现板块列表读取函数**

```python
def list_groups() -> list[dict]:
    """返回所有板块，按 order 排序。"""
    p = _groups_path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    if df.is_empty():
        return []
    # 按 order 排序返回
    return df.sort("order").to_dicts()


def _get_group_by_name(name: str) -> dict | None:
    """按名称查找板块（用于唯一性校验）。"""
    groups = list_groups()
    name = str(name).strip()
    for g in groups:
        if str(g.get("name", "")).strip() == name:
            return g
    return None


def _get_group_by_id(group_id: str) -> dict | None:
    """按 ID 查找板块。"""
    groups = list_groups()
    group_id = str(group_id).strip()
    for g in groups:
        if str(g.get("group_id", "")).strip() == group_id:
            return g
    return None
```

- [ ] **Step 3: 运行基础测试（文件不存在时返回空列表）**

Run: 手动验证函数逻辑（后续任务会添加完整测试）

---

### Task 1b: 实现板块 CRUD

- [ ] **Step 1: 实现创建板块函数**

```python
def create_group(name: str) -> list[dict]:
    """创建新板块。名称必须唯一。返回完整板块列表。"""
    name = str(name).strip()
    if not name:
        raise ValueError("板块名称不能为空")
    
    # 校验名称唯一性
    existing = _get_group_by_name(name)
    if existing is not None:
        raise ValueError(f"板块名称「{name}」已存在")
    
    p = _groups_path()
    if p.exists():
        df = pl.read_parquet(p)
    else:
        df = pl.DataFrame(schema={
            "group_id": pl.Utf8,
            "name": pl.Utf8,
            "order": pl.Int32,
            "created_at": pl.Utf8,
        })
    
    # 新板块 order 设为当前最大值 + 1
    max_order = df["order"].max() if not df.is_empty() and "order" in df.columns else -1
    new_order = int(max_order) + 1 if max_order is not None else 0
    
    new_row = pl.DataFrame({
        "group_id": [str(uuid.uuid4())],
        "name": [name],
        "order": [new_order],
        "created_at": [datetime.utcnow().isoformat(timespec="seconds")],
    })
    
    out = pl.concat([df, new_row], how="diagonal_relaxed")
    out.write_parquet(p)
    return list_groups()
```

- [ ] **Step 2: 实现重命名板块函数**

```python
def rename_group(group_id: str, new_name: str) -> list[dict]:
    """重命名板块。名称必须唯一。返回完整板块列表。"""
    group_id = str(group_id).strip()
    new_name = str(new_name).strip()
    if not new_name:
        raise ValueError("板块名称不能为空")
    
    # 校验板块存在
    existing_group = _get_group_by_id(group_id)
    if existing_group is None:
        raise ValueError(f"板块不存在")
    
    # 校验名称唯一性（排除自己）
    existing_by_name = _get_group_by_name(new_name)
    if existing_by_name is not None and str(existing_by_name.get("group_id")) != group_id:
        raise ValueError(f"板块名称「{new_name}」已存在")
    
    p = _groups_path()
    df = pl.read_parquet(p)
    
    # 更新名称
    df = df.with_columns(
        pl.when(pl.col("group_id") == group_id)
          .then(pl.lit(new_name))
          .otherwise(pl.col("name"))
          .alias("name")
    )
    
    df.write_parquet(p)
    return list_groups()
```

- [ ] **Step 3: 实现删除板块函数（级联删除板块内股票）**

```python
def delete_group(group_id: str) -> list[dict]:
    """删除板块，同时删除该板块内的所有股票关系。返回剩余板块列表。"""
    group_id = str(group_id).strip()
    
    # 删除板块元数据
    p = _groups_path()
    if p.exists():
        df = pl.read_parquet(p)
        df = df.filter(pl.col("group_id") != group_id)
        df.write_parquet(p)
    
    # 级联删除板块内股票
    items_p = _items_path()
    if items_p.exists():
        df_items = pl.read_parquet(items_p)
        df_items = df_items.filter(pl.col("group_id") != group_id)
        df_items.write_parquet(items_p)
    
    return list_groups()
```

- [ ] **Step 4: 实现板块排序函数**

```python
def reorder_groups(group_ids: list[str]) -> list[dict]:
    """更新板块排序。传入新的 group_id 顺序列表。返回排序后的板块列表。"""
    group_ids = [str(gid).strip() for gid in group_ids if gid and str(gid).strip()]
    if not group_ids:
        return list_groups()
    
    p = _groups_path()
    if not p.exists():
        return []
    
    df = pl.read_parquet(p)
    if df.is_empty():
        return []
    
    # 构建新的 order 映射
    order_map = {gid: i for i, gid in enumerate(group_ids)}
    
    # 更新 order，未在传入列表中的保持原 order 但移到后面
    max_new_order = len(group_ids)
    df = df.with_columns(
        pl.col("group_id").map_elements(
            lambda gid: order_map.get(str(gid), max_new_order + int(pl.col("order").fill_null(0))),
            return_dtype=pl.Int32
        ).alias("order")
    )
    
    df.write_parquet(p)
    return list_groups()
```

- [ ] **Step 5: 运行基础 CRUD 测试验证**

---

### Task 1c: 实现板块内股票管理

- [ ] **Step 1: 实现获取板块内股票函数**

```python
def list_group_items(group_id: str) -> list[dict]:
    """获取指定板块内的股票列表，按 order 排序。"""
    group_id = str(group_id).strip()
    p = _items_path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    df = df.filter(pl.col("group_id") == group_id)
    if df.is_empty():
        return []
    return df.sort("order").to_dicts()
```

- [ ] **Step 2: 实现添加股票到板块函数**

```python
def add_item(group_id: str, symbol: str, note: str = "") -> list[dict]:
    """添加股票到板块。已存在则先移除，重新插入到最前面。返回板块内股票列表。"""
    group_id = str(group_id).strip()
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise ValueError("股票代码不能为空")
    
    # 校验板块存在
    if _get_group_by_id(group_id) is None:
        raise ValueError("板块不存在")
    
    p = _items_path()
    if p.exists():
        df = pl.read_parquet(p)
        # 先移除该板块内已有的同一只股票
        df = df.filter(~((pl.col("group_id") == group_id) & (pl.col("symbol") == symbol)))
    else:
        df = pl.DataFrame(schema={
            "group_id": pl.Utf8,
            "symbol": pl.Utf8,
            "order": pl.Int32,
            "added_at": pl.Utf8,
            "note": pl.Utf8,
        })
    
    # 获取该板块当前最大 order
    group_items = df.filter(pl.col("group_id") == group_id)
    max_order = group_items["order"].max() if not group_items.is_empty() else -1
    new_order = int(max_order) + 1 if max_order is not None else 0
    
    new_row = pl.DataFrame({
        "group_id": [group_id],
        "symbol": [symbol],
        "order": [new_order],
        "added_at": [datetime.utcnow().isoformat(timespec="seconds")],
        "note": [str(note or "")],
    })
    
    out = pl.concat([df, new_row], how="diagonal_relaxed")
    out.write_parquet(p)
    return list_group_items(group_id)
```

- [ ] **Step 3: 实现批量添加股票函数**

```python
def add_items_batch(group_id: str, symbols: list[str], note: str = "") -> tuple[list[dict], int]:
    """批量添加股票到板块。返回（板块内股票列表，新增数量）。"""
    group_id = str(group_id).strip()
    symbols = [str(s).strip().upper() for s in symbols if s and str(s).strip()]
    if not symbols:
        return list_group_items(group_id), 0
    
    # 校验板块存在
    if _get_group_by_id(group_id) is None:
        raise ValueError("板块不存在")
    
    p = _items_path()
    if p.exists():
        df = pl.read_parquet(p)
        existing = set(df.filter(pl.col("group_id") == group_id)["symbol"].to_list())
    else:
        df = pl.DataFrame(schema={
            "group_id": pl.Utf8,
            "symbol": pl.Utf8,
            "order": pl.Int32,
            "added_at": pl.Utf8,
            "note": pl.Utf8,
        })
        existing = set()
    
    # 过滤已存在的，只新增
    new_symbols = [s for s in symbols if s not in existing]
    added_count = len(new_symbols)
    if added_count == 0:
        return list_group_items(group_id), 0
    
    # 获取该板块当前最大 order
    group_items = df.filter(pl.col("group_id") == group_id)
    max_order = group_items["order"].max() if not group_items.is_empty() else -1
    start_order = int(max_order) + 1 if max_order is not None else 0
    
    # 构建新行
    new_rows = []
    for i, symbol in enumerate(new_symbols):
        new_rows.append({
            "group_id": group_id,
            "symbol": symbol,
            "order": start_order + i,
            "added_at": datetime.utcnow().isoformat(timespec="seconds"),
            "note": str(note or ""),
        })
    
    out = pl.concat([df, pl.DataFrame(new_rows)], how="diagonal_relaxed")
    out.write_parquet(p)
    return list_group_items(group_id), added_count
```

- [ ] **Step 4: 实现从板块移除股票函数**

```python
def remove_item(group_id: str, symbol: str) -> list[dict]:
    """从板块移除股票。返回板块内剩余股票列表。"""
    group_id = str(group_id).strip()
    symbol = str(symbol).strip().upper()
    
    p = _items_path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    df = df.filter(~((pl.col("group_id") == group_id) & (pl.col("symbol") == symbol)))
    df.write_parquet(p)
    return list_group_items(group_id)
```

- [ ] **Step 5: 实现板块内股票排序函数**

```python
def reorder_items(group_id: str, symbols: list[str]) -> list[dict]:
    """更新板块内股票排序。传入新的 symbol 顺序列表。返回排序后的股票列表。"""
    group_id = str(group_id).strip()
    symbols = [str(s).strip().upper() for s in symbols if s and str(s).strip()]
    
    p = _items_path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    
    # 只修改该板块的
    group_mask = pl.col("group_id") == group_id
    df_group = df.filter(group_mask)
    
    if df_group.is_empty():
        return list_group_items(group_id)
    
    # 构建新的 order 映射
    order_map = {sym: i for i, sym in enumerate(symbols)}
    
    # 更新该板块内的 order
    max_new_order = len(symbols)
    df_group = df_group.with_columns(
        pl.col("symbol").map_elements(
            lambda sym: order_map.get(str(sym), max_new_order + int(pl.col("order").fill_null(0))),
            return_dtype=pl.Int32
        ).alias("order")
    )
    
    # 合并回去
    df_other = df.filter(~group_mask)
    out = pl.concat([df_other, df_group], how="diagonal_relaxed")
    out.write_parquet(p)
    return list_group_items(group_id)
```

- [ ] **Step 6: 运行股票管理测试验证**

---

### Task 1d: 实现实时订阅辅助函数和统计查询

- [ ] **Step 1: 实现获取所有板块股票函数（供 QuoteService 订阅）**

```python
def get_all_watchlist_symbols() -> list[str]:
    """获取所有板块中的所有股票（去重），用于实时行情订阅。"""
    p = _items_path()
    if not p.exists():
        return []
    df = pl.read_parquet(p)
    if df.is_empty():
        return []
    # 去重返回
    return list(set(df["symbol"].to_list()))
```

- [ ] **Step 2: 实现获取板块详情（含股票数量）**

```python
def get_groups_with_stats() -> list[dict]:
    """获取所有板块列表，包含股票数量统计。"""
    groups = list_groups()
    
    # 获取各板块股票数量
    items_p = _items_path()
    if items_p.exists():
        df_items = pl.read_parquet(items_p)
        if not df_items.is_empty():
            counts = df_items.group_by("group_id").len(name="item_count").to_dicts()
            count_map = {c["group_id"]: c["item_count"] for c in counts}
        else:
            count_map = {}
    else:
        count_map = {}
    
    # 组装返回
    result = []
    for g in groups:
        result.append({
            **g,
            "item_count": count_map.get(g["group_id"], 0),
        })
    return result
```

- [ ] **Step 3: 运行完整服务测试**

---

## Task 2: 后端板块 API (watchlist_groups.py)

**Files:**
- Create: `backend/app/api/watchlist_groups.py`
- Modify: `backend/app/main.py` (注册路由)

**Interfaces:**
- Produces: `GET /api/watchlist-groups` (含平均涨跌幅)
- Produces: `POST /api/watchlist-groups`
- Produces: `PATCH /api/watchlist-groups/{group_id}`
- Produces: `DELETE /api/watchlist-groups/{group_id}`
- Produces: `POST /api/watchlist-groups/order`
- Produces: `GET /api/watchlist-groups/{group_id}/items`
- Produces: `GET /api/watchlist-groups/{group_id}/items/enriched`
- Produces: `POST /api/watchlist-groups/{group_id}/items`
- Produces: `POST /api/watchlist-groups/{group_id}/items/batch`
- Produces: `DELETE /api/watchlist-groups/{group_id}/items/{symbol}`
- Produces: `POST /api/watchlist-groups/{group_id}/items/order`

**设计参考:** 完全复用 `api/watchlist.py` 的模式

---

### Task 2a: 实现基础 API 结构和板块 CRUD

- [ ] **Step 1: 创建 API 文件基础结构**

```python
"""自选板块 API。"""
from __future__ import annotations

import logging
import math
import time
from datetime import date

import anyio
import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist-groups", tags=["watchlist-groups"])

# 复用 watchlist.py 的 _with_names 模式
def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        name_by_symbol = request.app.state.repo.get_name_map([r.get("symbol") for r in rows])
        if not name_by_symbol:
            return rows
        return [{**row, "name": name_by_symbol.get(row.get("symbol"))} for row in rows]
    except Exception as e:
        logger.debug("attach watchlist group names failed: %s", e)
        return rows
```

- [ ] **Step 2: 实现板块列表 API（含平均涨跌幅占位）**

```python
# 请求/响应模型
class CreateGroupRequest(BaseModel):
    name: str


class RenameGroupRequest(BaseModel):
    name: str


class ReorderGroupsRequest(BaseModel):
    group_ids: list[str]


@router.get("")
def list_all_groups(request: Request):
    """获取所有板块列表。"""
    from app.services import watchlist_groups
    groups = watchlist_groups.get_groups_with_stats()
    
    # TODO: 添加平均涨跌幅计算 (后续任务)
    for g in groups:
        g["avg_change_pct"] = None
        g["avg_change_pct_weighted"] = None
    
    return {"groups": groups}


@router.post("")
def create_group(req: CreateGroupRequest, request: Request):
    """创建新板块。"""
    from app.services import watchlist_groups
    try:
        groups = watchlist_groups.create_group(req.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    
    # 重新获取带统计的列表
    groups = watchlist_groups.get_groups_with_stats()
    for g in groups:
        g["avg_change_pct"] = None
        g["avg_change_pct_weighted"] = None
    return {"groups": groups}


@router.patch("/{group_id}")
def rename_group(group_id: str, req: RenameGroupRequest, request: Request):
    """重命名板块。"""
    from app.services import watchlist_groups
    try:
        groups = watchlist_groups.rename_group(group_id, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    
    groups = watchlist_groups.get_groups_with_stats()
    for g in groups:
        g["avg_change_pct"] = None
        g["avg_change_pct_weighted"] = None
    return {"groups": groups}


@router.delete("/{group_id}")
def delete_group(group_id: str, request: Request):
    """删除板块（级联删除股票）。"""
    from app.services import watchlist_groups
    try:
        groups = watchlist_groups.delete_group(group_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    
    groups = watchlist_groups.get_groups_with_stats()
    for g in groups:
        g["avg_change_pct"] = None
        g["avg_change_pct_weighted"] = None
    return {"groups": groups}


@router.post("/order")
def reorder_groups(req: ReorderGroupsRequest, request: Request):
    """更新板块排序。"""
    from app.services import watchlist_groups
    groups = watchlist_groups.reorder_groups(req.group_ids)
    
    groups = watchlist_groups.get_groups_with_stats()
    for g in groups:
        g["avg_change_pct"] = None
        g["avg_change_pct_weighted"] = None
    return {"groups": groups}
```

- [ ] **Step 3: 实现板块内股票 API**

```python
# 股票请求模型
class AddItemRequest(BaseModel):
    symbol: str
    note: str = ""


class AddItemsBatchRequest(BaseModel):
    symbols: list[str]
    note: str = ""


class ReorderItemsRequest(BaseModel):
    symbols: list[str]


@router.get("/{group_id}/items")
def list_group_items(group_id: str, request: Request):
    """获取板块内股票列表。"""
    from app.services import watchlist_groups
    try:
        items = watchlist_groups.list_group_items(group_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"items": _with_names(items, request)}


@router.post("/{group_id}/items")
def add_item(group_id: str, req: AddItemRequest, request: Request):
    """添加股票到板块。"""
    from app.services import watchlist_groups
    try:
        items = watchlist_groups.add_item(group_id, req.symbol, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"items": _with_names(items, request)}


@router.post("/{group_id}/items/batch")
def add_items_batch(group_id: str, req: AddItemsBatchRequest, request: Request):
    """批量添加股票到板块。"""
    from app.services import watchlist_groups
    try:
        items, added = watchlist_groups.add_items_batch(group_id, req.symbols, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"items": _with_names(items, request), "added": added}


@router.delete("/{group_id}/items/{symbol}")
def remove_item(group_id: str, symbol: str, request: Request):
    """从板块移除股票。"""
    from app.services import watchlist_groups
    items = watchlist_groups.remove_item(group_id, symbol)
    return {"items": _with_names(items, request)}


@router.post("/{group_id}/items/order")
def reorder_items(group_id: str, req: ReorderItemsRequest, request: Request):
    """更新板块内股票排序。"""
    from app.services import watchlist_groups
    items = watchlist_groups.reorder_items(group_id, req.symbols)
    return {"items": _with_names(items, request)}
```

- [ ] **Step 4: 注册 API 路由到 main.py**

修改 `backend/app/main.py`，在导入区域添加：

```python
from app.api import watchlist_groups
```

在 app.include_router 区域添加：

```python
app.include_router(watchlist_groups.router)
```

- [ ] **Step 5: 运行基础 API 测试**

---

### Task 2b: 实现 enriched 数据 API（复用 watchlist 模式）

- [ ] **Step 1: 添加板块 enriched API（完全复用 watchlist 的 enriched 逻辑）**

```python
# 板块 enriched 列（复用 watchlist 的）
_WATCHLIST_COLS = [
    "symbol", "close", "change_pct", "change_amount", "amount",
    "turnover_rate",
    "amplitude", "annual_vol_20d",
    "vol_ratio_5d",
    "ma5", "ma10", "ma20", "ma60",
    "vol_ma5", "vol_ma10",
    "high_60d", "low_60d",
    "rsi_6", "rsi_14", "rsi_24",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
    "atr_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "consecutive_limit_ups", "consecutive_limit_downs",
    "signal_limit_up", "signal_limit_down", "signal_volume_surge",
    "signal_ma_golden_5_20", "signal_macd_golden", "signal_n_day_high",
    "signal_boll_breakout_upper", "signal_ma20_breakout",
    "signal_ma_dead_5_20", "signal_macd_dead", "signal_n_day_low",
    "signal_boll_breakdown_lower", "signal_ma20_breakdown",
]


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 ext 列（复用 watchlist 的）。"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if config_id and field_name:
            result.append((config_id, field_name))
    return result


@router.get("/{group_id}/items/enriched")
def group_items_enriched(
    group_id: str,
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列"),
):
    """获取板块内股票 enriched 数据（完全复用 watchlist 的逻辑）。"""
    from app.services import watchlist_groups
    
    t0 = time.perf_counter()
    
    repo = request.app.state.repo
    items = watchlist_groups.list_group_items(group_id)
    symbols = [r["symbol"] for r in items]
    
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0}
    
    # 按资产类型拆分
    etf_set = repo.get_etf_symbol_set()
    index_set = repo.get_index_symbol_set()
    etf_symbols = [s for s in symbols if s in etf_set]
    index_symbols = [s for s in symbols if s not in etf_set and s in index_set]
    stock_symbols = [s for s in symbols if s not in etf_set and s not in index_set]
    
    df_e, cache_date = repo.get_enriched_latest()
    
    # 以板块股票列表为主表 LEFT JOIN enriched
    df = pl.DataFrame()
    if stock_symbols:
        watchlist_df = pl.DataFrame({"symbol": stock_symbols})
        if df_e.is_empty():
            df = watchlist_df
        else:
            df = watchlist_df.join(df_e, on="symbol", how="left")
    
    # ETF 合并
    etf_date = None
    if etf_symbols:
        df_etf_all, etf_date = repo.get_enriched_latest_asset("etf")
        etf_watchlist_df = pl.DataFrame({"symbol": etf_symbols})
        if not df_etf_all.is_empty():
            df_etf = etf_watchlist_df.join(df_etf_all, on="symbol", how="left")
        else:
            df_etf = etf_watchlist_df
        df = df_etf if df.is_empty() else pl.concat([df, df_etf], how="diagonal_relaxed")
    
    # 指数合并
    index_date = None
    if index_symbols:
        df_idx_all, index_date = repo.get_enriched_latest_asset("index")
        idx_watchlist_df = pl.DataFrame({"symbol": index_symbols})
        if not df_idx_all.is_empty():
            df_idx = idx_watchlist_df.join(df_idx_all, on="symbol", how="left")
        else:
            df_idx = idx_watchlist_df
        df = df_idx if df.is_empty() else pl.concat([df, df_idx], how="diagonal_relaxed")
    
    # as_of 取三类中较旧的
    dates = [d for d in (cache_date if stock_symbols else None, etf_date, index_date) if d is not None]
    as_of = min(dates) if dates else None
    if df.is_empty():
        return {"rows": [], "as_of": str(as_of) if as_of else None, "elapsed_ms": 0}
    
    # JOIN float_shares 和 name
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "float_shares" in df_i.columns:
        df = df.join(df_i.select(["symbol", "float_shares"]), on="symbol", how="left")
    name_map = repo.get_name_map(df["symbol"].to_list())
    df = df.with_columns(
        pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name")
    )
    
    # 标注资产类型
    asset_map = {**{s: "etf" for s in etf_symbols}, **{s: "index" for s in index_symbols}}
    df = df.with_columns(
        pl.col("symbol").replace_strict(asset_map, default="stock", return_dtype=pl.Utf8).alias("asset_type")
    )
    
    # 选择列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares", "asset_type"] if c in df.columns]
    df = df.select(keep)
    
    # 动态 JOIN ext 列（复用 watchlist 逻辑）
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if ext_specs:
        db = repo.store.db
        from app.services.ext_data import ExtConfigStore
        
        ext_store = ExtConfigStore(repo.store.data_dir)
        configs = {c.id: c for c in ext_store.load_all()}
        
        for config_id, field_name in ext_specs:
            ext_col_name = f"{config_id}__{field_name}"
            try:
                cfg = configs.get(config_id)
                if cfg:
                    from app.api.ext_data import _read_ext_dataframe
                    ext_df, _ = _read_ext_dataframe(cfg, repo.store.data_dir)
                else:
                    from app.db_safe import quote_ident
                    ext_df = pl.from_arrow(db.query(
                        f"SELECT symbol, {quote_ident(field_name)} FROM ext_{config_id}"
                    ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = (
                        ext_df
                        .select(["symbol", field_name])
                        .unique(subset=["symbol"], keep="last")
                        .rename({field_name: ext_col_name})
                    )
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
            except Exception:
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        from app.api.ext_data import _read_ext_dataframe
                        ext_df, _ = _read_ext_dataframe(cfg, repo.store.data_dir)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = (
                                ext_df
                                .select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug("ext join fallback failed for %s.%s: %s", config_id, field_name, e2)
    
    # sanitize NaN/Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])
    
    # 按板块内 order 排序
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(
        pl.col("symbol").map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32).alias("_sort_order")
    )
    df = df.sort("_sort_order").drop("_sort_order")
    
    rows = df.to_dicts()
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(as_of) if as_of else None, "elapsed_ms": elapsed}
```

- [ ] **Step 2: 运行 API 测试验证**

---

## Task 3: Preferences 集成（全局设置存储）

**Files:**
- Modify: `backend/app/services/preferences.py`

**Interfaces:**
- Produces: `get_watchlist_groups_view_mode()` / `set_watchlist_groups_view_mode()`
- Produces: `get_watchlist_groups_display_style()` / `set_watchlist_groups_display_style()`
- Produces: `get_watchlist_groups_avg_pct_mode()` / `set_watchlist_groups_avg_pct_mode()`

---

### Task 3a: 添加板块全局设置存取函数

- [ ] **Step 1: 在 preferences.py 末尾添加板块设置函数**

```python
# ===== 自选板块设置 =====

def get_watchlist_groups_view_mode() -> str:
    """获取板块视图模式: 'sidebar' (侧边栏+单板块) 或 'cards' (多板块卡片)。"""
    return str(load().get("watchlist_groups_view_mode", "sidebar")).strip()


def set_watchlist_groups_view_mode(mode: str) -> str:
    """保存板块视图模式。"""
    mode = str(mode).strip()
    if mode not in ["sidebar", "cards"]:
        mode = "sidebar"
    save({"watchlist_groups_view_mode": mode})
    return mode


def get_watchlist_groups_display_style() -> str:
    """获取板块展示样式: 'compact' (精简), 'standard' (标准), 'detailed' (详细)。"""
    return str(load().get("watchlist_groups_display_style", "standard")).strip()


def set_watchlist_groups_display_style(style: str) -> str:
    """保存板块展示样式。"""
    style = str(style).strip()
    if style not in ["compact", "standard", "detailed"]:
        style = "standard"
    save({"watchlist_groups_display_style": style})
    return style


def get_watchlist_groups_avg_pct_mode() -> str:
    """获取平均涨跌幅计算模式: 'simple' (算术平均) 或 'weighted' (加权平均)。"""
    return str(load().get("watchlist_groups_avg_pct_mode", "simple")).strip()


def set_watchlist_groups_avg_pct_mode(mode: str) -> str:
    """保存平均涨跌幅计算模式。"""
    mode = str(mode).strip()
    if mode not in ["simple", "weighted"]:
        mode = "simple"
    save({"watchlist_groups_avg_pct_mode": mode})
    return mode
```

- [ ] **Step 2: 在 settings.py API 中暴露这些设置（后续任务）**

---

## Task 4: 实时行情订阅集成

**Files:**
- Modify: `backend/app/services/quote_service.py`

**Interfaces:**
- 修改: 把板块股票合并到实时订阅池

---

### Task 4a: 集成板块股票到 QuoteService

- [ ] **Step 1: 查找 quote_service.py 中获取自选股的位置**

参考 `get_realtime_watchlist_symbols()` 在 preferences.py 中的实现，找到 QuoteService 中调用它的位置。

- [ ] **Step 2: 修改订阅池获取逻辑，合并板块股票**

在相关位置添加：

```python
from app.services import watchlist_groups

# ... 在获取订阅股票的位置 ...
watchlist_symbols = preferences.get_realtime_watchlist_symbols()
group_symbols = watchlist_groups.get_all_watchlist_symbols()
# 合并去重
all_symbols = list(set(watchlist_symbols + group_symbols))
```

（具体修改位置需要查看 quote_service.py 的实际代码结构）

- [ ] **Step 3: 运行实时刷新测试验证**

---

## Task 5: 前端 API 类型和路由 (api.ts)

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Produces: TypeScript types for WatchlistGroup, GroupItem
- Produces: API functions for all endpoints

---

### Task 5a: 添加板块相关类型定义

- [ ] **Step 1: 在 api.ts 中添加板块类型**

```typescript
// ===== 自选板块 =====
export interface WatchlistGroup {
  group_id: string;
  name: string;
  order: number;
  avg_change_pct?: number | null;
  avg_change_pct_weighted?: number | null;
  item_count: number;
  created_at: string;
}

export interface WatchlistGroupItem {
  group_id: string;
  symbol: string;
  order: number;
  added_at: string;
  note?: string;
  name?: string | null;
}
```

- [ ] **Step 2: 添加 API 函数**

```typescript
// 在 api 对象中添加
export const api = {
  // ... 现有 API ...
  
  watchlistGroups: {
    list: () =>
      request<{ groups: WatchlistGroup[] }>("/api/watchlist-groups"),
    
    create: (name: string) =>
      request<{ groups: WatchlistGroup[] }>("/api/watchlist-groups", {
        method: "POST",
        body: JSON.stringify({ name }),
      }),
    
    rename: (groupId: string, name: string) =>
      request<{ groups: WatchlistGroup[] }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    
    delete: (groupId: string) =>
      request<{ groups: WatchlistGroup[] }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}`, {
        method: "DELETE",
      }),
    
    reorder: (groupIds: string[]) =>
      request<{ groups: WatchlistGroup[] }>("/api/watchlist-groups/order", {
        method: "POST",
        body: JSON.stringify({ group_ids: groupIds }),
      }),
    
    listItems: (groupId: string) =>
      request<{ items: WatchlistGroupItem[] }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}/items`),
    
    listItemsEnriched: (groupId: string, extColumns?: string) =>
      request<{ rows: any[]; as_of: string | null; elapsed_ms: number }>(
        extColumns
          ? `/api/watchlist-groups/${encodeURIComponent(groupId)}/items/enriched?ext_columns=${encodeURIComponent(extColumns)}`
          : `/api/watchlist-groups/${encodeURIComponent(groupId)}/items/enriched`
      ),
    
    addItem: (groupId: string, symbol: string, note?: string) =>
      request<{ items: WatchlistGroupItem[] }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}/items`, {
        method: "POST",
        body: JSON.stringify({ symbol, note: note || "" }),
      }),
    
    addItemsBatch: (groupId: string, symbols: string[], note?: string) =>
      request<{ items: WatchlistGroupItem[]; added: number }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}/items/batch`, {
        method: "POST",
        body: JSON.stringify({ symbols, note: note || "" }),
      }),
    
    removeItem: (groupId: string, symbol: string) =>
      request<{ items: WatchlistGroupItem[] }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}/items/${encodeURIComponent(symbol)}`, {
        method: "DELETE",
      }),
    
    reorderItems: (groupId: string, symbols: string[]) =>
      request<{ items: WatchlistGroupItem[] }>(`/api/watchlist-groups/${encodeURIComponent(groupId)}/items/order`, {
        method: "POST",
        body: JSON.stringify({ symbols }),
      }),
  },
};
```

- [ ] **Step 3: 更新 Preferences 相关类型（如果需要）**

---

## Task 6: 前端路由和页面框架 (WatchlistGroups.tsx)

**Files:**
- Create: `frontend/src/pages/WatchlistGroups.tsx`
- Modify: `frontend/src/router.tsx`

**Interfaces:**
- 新增: `/watchlist-groups` 路由

---

### Task 6a: 添加路由和页面框架

- [ ] **Step 1: 在 router.tsx 中添加路由**

在 lazy 导入区域添加：

```typescript
const WatchlistGroups = lazy(() => import("./pages/WatchlistGroups").then(m => ({ default: m.WatchlistGroups })));
```

在 children 路由数组中添加：

```typescript
{ path: "watchlist-groups", element: <WatchlistGroups /> },
```

- [ ] **Step 2: 创建页面基础框架**

```typescript
import React, { useState, useEffect, useMemo } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Plus, Settings, Layout, Grid, List, Trash2, Edit2, GripVertical, ChevronDown, ChevronUp } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { toast } from "@/components/Toast";
import { api, WatchlistGroup, WatchlistGroupItem } from "@/lib/api";
import { useQuoteStatus } from "@/lib/useQuoteStatus";
import { useSettings } from "@/lib/useSharedQueries";

// 组件导入
// TODO: 导入 StockDataTable, StockSearchBox 等复用组件

export function WatchlistGroups() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  
  // 状态
  const [viewMode, setViewMode] = useState<"sidebar" | "cards">("sidebar");
  const [displayStyle, setDisplayStyle] = useState<"compact" | "standard" | "detailed">("standard");
  const [avgPctMode, setAvgPctMode] = useState<"simple" | "weighted">("simple");
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [renameDialogOpen, setRenameDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [currentGroupForDialog, setCurrentGroupForDialog] = useState<WatchlistGroup | null>(null);
  const [newGroupName, setNewGroupName] = useState("");
  
  // Queries
  const groupsQuery = useQuery({
    queryKey: ["watchlistGroups"],
    queryFn: () => api.watchlistGroups.list(),
  });
  
  // 选中板块的 enriched 数据
  const selectedGroupItemsQuery = useQuery({
    queryKey: ["watchlistGroupItemsEnriched", selectedGroupId],
    queryFn: () => selectedGroupId ? api.watchlistGroups.listItemsEnriched(selectedGroupId) : null,
    enabled: !!selectedGroupId,
  });
  
  // 实时行情
  const quoteStatus = useQuoteStatus();
  
  // 选中第一个板块
  useEffect(() => {
    if (groupsQuery.data?.groups?.length && !selectedGroupId) {
      setSelectedGroupId(groupsQuery.data.groups[0].group_id);
    }
  }, [groupsQuery.data]);
  
  // Mutations
  const createMutation = useMutation({
    mutationFn: (name: string) => api.watchlistGroups.create(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlistGroups"] });
      toast("板块创建成功");
      setCreateDialogOpen(false);
      setNewGroupName("");
    },
    onError: (e: any) => toast(e.message || "创建失败", "error"),
  });
  
  // TODO: 添加更多 mutations (rename, delete, reorder, etc.)
  
  // 渲染函数
  const renderSidebarView = () => (
    <div className="flex h-full">
      {/* 侧边栏: 板块列表 */}
      <div className="w-64 border-r bg-card p-4 overflow-y-auto">
        {/* 头部 */}
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">板块</h2>
          <Button size="sm" onClick={() => setCreateDialogOpen(true)}>
            <Plus className="w-4 h-4 mr-1" /> 新建
          </Button>
        </div>
        
        {/* 板块列表 */}
        <div className="space-y-2">
          {groupsQuery.data?.groups?.map((group) => (
            <button
              key={group.group_id}
              onClick={() => setSelectedGroupId(group.group_id)}
              className={`w-full text-left p-3 rounded-lg transition-colors ${
                selectedGroupId === group.group_id ? "bg-primary text-primary-foreground" : "hover:bg-accent"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium truncate">{group.name}</span>
                <span className="text-xs opacity-70">
                  {/* TODO: 显示平均涨跌幅 */}
                  {group.item_count} 只
                </span>
              </div>
            </button>
          ))}
        </div>
      </div>
      
      {/* 主内容: 板块详情 */}
      <div className="flex-1 overflow-auto p-6">
        {/* TODO: 实现板块详情 */}
        {selectedGroupId ? (
          <div>板块详情</div>
        ) : (
          <div className="text-center text-muted-foreground py-12">请选择一个板块</div>
        )}
      </div>
    </div>
  );
  
  const renderCardsView = () => (
    <div className="p-6">
      {/* 头部 */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">自选板块</h1>
        <div className="flex items-center gap-2">
          {/* TODO: 视图切换、排序等 */}
          <Button onClick={() => setCreateDialogOpen(true)}>
            <Plus className="w-4 h-4 mr-2" /> 新建板块
          </Button>
        </div>
      </div>
      
      {/* 板块卡片网格 */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {groupsQuery.data?.groups?.map((group) => (
          <div key={group.group_id} className="border rounded-lg bg-card">
            <div className="p-4 border-b flex items-center justify-between">
              <h3 className="font-semibold">{group.name}</h3>
              {/* TODO: 板块操作菜单 */}
            </div>
            <div className="p-4">
              {/* TODO: 股票列表（默认显示前 5 只） */}
              <div className="text-sm text-muted-foreground">
                {group.item_count} 只股票
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
  
  return (
    <div className="h-full">
      {/* 视图切换栏 */}
      <div className="border-b px-6 py-3 flex items-center justify-between">
        <Tabs value={viewMode} onValueChange={(v) => setViewMode(v as any)}>
          <TabsList>
            <TabsTrigger value="sidebar">
              <List className="w-4 h-4 mr-2" /> 侧边栏
            </TabsTrigger>
            <TabsTrigger value="cards">
              <Grid className="w-4 h-4 mr-2" /> 卡片
            </TabsTrigger>
          </TabsList>
        </Tabs>
        
        {/* 全局设置 */}
        <div className="flex items-center gap-2">
          {/* TODO: 展示样式切换、平均涨跌幅模式切换 */}
        </div>
      </div>
      
      {/* 主内容 */}
      {viewMode === "sidebar" ? renderSidebarView() : renderCardsView()}
      
      {/* 创建板块 Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建板块</DialogTitle>
          </DialogHeader>
          <Input
            placeholder="输入板块名称"
            value={newGroupName}
            onChange={(e) => setNewGroupName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createMutation.mutate(newGroupName)}
          />
          <DialogFooter>
            <Button variant="secondary" onClick={() => setCreateDialogOpen(false)}>取消</Button>
            <Button 
              onClick={() => createMutation.mutate(newGroupName)}
              disabled={!newGroupName.trim() || createMutation.isPending}
            >
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      
      {/* TODO: 重命名和删除 Dialog */}
    </div>
  );
}
```

- [ ] **Step 3: 运行基础页面测试**

---

## Task 7: 前端完整功能实现

**Files:**
- Modify: `frontend/src/pages/WatchlistGroups.tsx`

**内容:**
- 完整实现侧边栏视图
- 完整实现卡片视图
- 实现股票添加/移除/排序
- 实现平均涨跌幅计算
- 集成实时行情刷新
- 集成全局设置持久化

---

### Task 7a-7z: （任务会在实际执行时分解为更细的步骤）

---

## Task 8: 平均涨跌幅计算实现

**Files:**
- Modify: `backend/app/api/watchlist_groups.py` (添加平均涨跌幅计算)
- Modify: `frontend/src/pages/WatchlistGroups.tsx` (实时重新计算)

**内容:**
- 后端初始计算平均涨跌幅
- 前端收到实时行情时重新计算

---

## Task 9: Settings API 集成（全局设置）

**Files:**
- Modify: `backend/app/api/settings.py` (暴露板块设置)
- Modify: `frontend/src/lib/api.ts` (添加设置 API)
- Modify: `frontend/src/pages/Settings.tsx` (可选：添加板块设置 UI)

---

## Task 10: 完整测试和优化

**内容:**
- 端到端测试
- 性能测试（多板块、多股票）
- 边界情况处理（空板块、重名、删除等）
- UI/UX 优化

---

## 自我审查检查清单

- [ ] **Spec 覆盖**: 所有 spec 中的需求都有对应的任务
- [ ] **无占位符**: 所有步骤都有实际代码，无 TODO/占位
- [ ] **类型一致**: 前后端类型定义一致
- [ ] **复用原则**: 最大程度复用了现有代码
- [ ] **向后兼容**: 不破坏现有功能
- [ ] **数据安全**: 删除操作安全，无路径穿越等风险

---

## 执行选项

Plan complete and saved to `docs/superpowers/plans/2026-08-15-watchlist-groups-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
