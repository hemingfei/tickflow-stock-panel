# 自选板块 UI 优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化自选板块的用户界面，包括：将添加股票功能直接嵌入页面右上角，设置按钮也放在右上角，侧边栏平均涨幅始终显示。

**Architecture:** 仅修改 `WatchlistGroups.tsx` 单个文件，保持现有数据流程和架构不变，只调整 UI 布局和交互。

**Tech Stack:** React + TypeScript + TanStack Query + Tailwind CSS + Lucide Icons

**Spec:** 基于聊天中的设计方案实施。

---

## 全局约束

- 保持现有功能完整性
- 遵循代码库现有风格和模式
- 不引入新的依赖包

---

### Task 1: 预加载所有板块数据，使平均涨幅始终显示

**Files:**
- Modify: `frontend/src/pages/WatchlistGroups.tsx`

**Interfaces:**
- Consumes: 现有的 `groupsQuery` 和 `allGroupItems` 查询
- Produces: 侧边栏始终显示所有板块的平均涨幅

---

- [ ] **Step 1: 修改 `allGroupItems` 查询，使其在侧边栏视图也启用**

```typescript
// 获取所有板块的 enriched 数据（用于卡片视图和侧边栏）
const { data: allGroupItems } = useQuery({
  queryKey: ['watchlist-groups-all-items', groupsQuery.data?.groups?.map(g => g.group_id).join(',')],
  queryFn: async () => {
    if (!groupsQuery.data?.groups?.length) return {}
    const result: Record<string, any[]> = {}
    for (const group of groupsQuery.data.groups) {
      try {
        const data = await api.watchlistGroups.listItemsEnriched(group.group_id)
        result[group.group_id] = data.rows || []
      } catch {
        result[group.group_id] = []
      }
    }
    return result
  },
  enabled: !!groupsQuery.data?.groups?.length, // 移除 viewMode 限制
  staleTime: 60_000,
})
```

- [ ] **Step 2: 更新侧边栏渲染，使用 `allGroupItems` 而不是仅选中板块的数据**

```typescript
{groupsQuery.data?.groups?.map((group) => {
  const rows = allGroupItems?.[group.group_id]
  const avgChange = rows ? calculateGroupAvgChange(group, { [group.group_id]: rows }) : { simple: null, weighted: null }
  const displayChange = avgPctMode === 'simple' ? avgChange.simple : avgChange.weighted

  return (
    <button
      key={group.group_id}
      onClick={() => setSelectedGroupId(group.group_id)}
      className={cn(
        'w-full text-left p-3 rounded-btn transition-colors',
        selectedGroupId === group.group_id ? 'bg-elevated text-foreground' : 'hover:bg-elevated/50'
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-medium truncate">{group.name}</span>
        <div className="flex items-center gap-2">
          {displayChange != null && (
            <span className={cn('text-xs font-medium', displayChange >= 0 ? 'text-bull' : 'text-bear')}>{fmtPct(displayChange)}</span>
          )}
          <span className="text-xs opacity-70">{group.item_count} 只</span>
        </div>
      </div>
    </button>
  )
})}
```

- [ ] **Step 3: 运行 TypeScript 检查确保没有错误**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: 提交更改**

```bash
git add frontend/src/pages/WatchlistGroups.tsx
git commit -m "feat: 侧边栏板块平均涨幅始终显示"
```

---

### Task 2: 将添加股票搜索框移到页面右上角

**Files:**
- Modify: `frontend/src/pages/WatchlistGroups.tsx`

---

- [ ] **Step 1: 修改 `renderSidebarView` 中的右上角区域**

```typescript
{selectedGroupId ? (
  <div className="p-6">
    <div className="flex items-center justify-between mb-6">
      <h1 className="text-2xl font-bold">{groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)?.name}</h1>
      <div className="flex items-center gap-2">
        {/* 将 StockSearchBox 直接嵌入这里 */}
        <StockSearchBox
          onPreview={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name) }}
          existingSymbols={selectedGroupItemsQuery.data?.rows?.map(r => r.symbol) ?? []}
          onAdd={(sym) => addItemMutation.mutate({ groupId: selectedGroupId, symbol: sym })}
        />
        {/* 移除原来的"添加股票"按钮 */}
        {/* 保留其他操作按钮，稍后会移到设置菜单 */}
        <button
          type="button"
          onClick={() => {
            const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
            if (group) {
              setCurrentGroupForDialog(group)
              setNewGroupName(group.name)
              setRenameDialogOpen(true)
            }
          }}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-elevated hover:bg-elevated/80 transition-colors"
        >
          <Edit2 className="h-4 w-4" />
          重命名
        </button>
        <button
          type="button"
          onClick={() => {
            const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
            if (group) {
              setCurrentGroupForDialog(group)
              setDeleteDialogOpen(true)
            }
          }}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-danger/10 text-danger hover:bg-danger/20 transition-colors"
        >
          <Trash2 className="h-4 w-4" />
          删除
        </button>
      </div>
    </div>
    {/* ... 其他部分保持不变 */}
  </div>
) : (
  <EmptyState title="请选择一个板块" hint="从左侧选择或创建一个新的板块" />
)}
```

- [ ] **Step 2: 移除原来的 `addStockDialogOpen` 相关代码**
  - 移除状态变量 `const [addStockDialogOpen, setAddStockDialogOpen] = useState(false)`
  - 移除对应的 Modal 弹窗代码

- [ ] **Step 3: 运行 TypeScript 检查**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: 提交更改**

```bash
git add frontend/src/pages/WatchlistGroups.tsx
git commit -m "feat: 将添加股票搜索框移到页面右上角"
```

---

### Task 3: 在页面右上角添加设置按钮，包含重命名和删除功能

**Files:**
- Modify: `frontend/src/pages/WatchlistGroups.tsx`

---

- [ ] **Step 1: 导入所需图标**
  确保导入 `Settings` 图标（如果还没导入的话）

```typescript
import { Plus, List, Grid, Trash2, Edit2, X, Search, Check, Settings } from 'lucide-react'
```

- [ ] **Step 2: 添加下拉菜单状态和组件**

```typescript
const [settingsMenuOpen, setSettingsMenuOpen] = useState(false)
const settingsMenuRef = useRef<HTMLDivElement>(null)

// 点击外部关闭菜单
useEffect(() => {
  function handleClickOutside(event: MouseEvent) {
    if (settingsMenuRef.current && !settingsMenuRef.current.contains(event.target as Node)) {
      setSettingsMenuOpen(false)
    }
  }
  document.addEventListener('mousedown', handleClickOutside)
  return () => document.removeEventListener('mousedown', handleClickOutside)
}, [])
```

- [ ] **Step 3: 修改右上角区域，用设置菜单替换单独的按钮**

```typescript
<div className="flex items-center gap-2">
  <StockSearchBox
    onPreview={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name) }}
    existingSymbols={selectedGroupItemsQuery.data?.rows?.map(r => r.symbol) ?? []}
    onAdd={(sym) => addItemMutation.mutate({ groupId: selectedGroupId, symbol: sym })}
  />
  
  {/* 设置按钮和下拉菜单 */}
  <div className="relative" ref={settingsMenuRef}>
    <button
      type="button"
      onClick={() => setSettingsMenuOpen(!settingsMenuOpen)}
      className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-elevated hover:bg-elevated/80 transition-colors"
    >
      <Settings className="h-4 w-4" />
      设置
    </button>
    
    <AnimatePresence>
      {settingsMenuOpen && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          className="absolute right-0 top-full mt-1 w-48 bg-surface border border-border rounded-btn shadow-xl z-50"
        >
          <button
            type="button"
            onClick={() => {
              setSettingsMenuOpen(false)
              const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
              if (group) {
                setCurrentGroupForDialog(group)
                setNewGroupName(group.name)
                setRenameDialogOpen(true)
              }
            }}
            className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
          >
            <Edit2 className="h-3.5 w-3.5" />
            重命名板块
          </button>
          <button
            type="button"
            onClick={() => {
              setSettingsMenuOpen(false)
              const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
              if (group) {
                setCurrentGroupForDialog(group)
                setDeleteDialogOpen(true)
              }
            }}
            className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2 text-danger"
          >
            <Trash2 className="h-3.5 w-3.5" />
            删除板块
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  </div>
</div>
```

- [ ] **Step 4: 运行 TypeScript 检查**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 5: 提交更改**

```bash
git add frontend/src/pages/WatchlistGroups.tsx
git commit -m "feat: 添加设置按钮整合重命名和删除功能"
```

---

### Task 4: 最终测试和验证

**Files:** 不涉及文件修改

---

- [ ] **Step 1: 启动开发服务器并测试所有功能**

```bash
# 启动服务（根据项目实际情况）
cd frontend && npm run dev
```

- [ ] **Step 2: 手动测试清单**
  - [ ] 侧边栏所有板块平均涨幅正常显示
  - [ ] 右上角搜索框可以正常添加股票
  - [ ] 设置按钮下拉菜单可以正常打开和关闭
  - [ ] 重命名功能正常工作
  - [ ] 删除功能正常工作
  - [ ] 卡片视图功能不受影响

- [ ] **Step 3: 运行项目现有测试（如果有）**

```bash
cd frontend && npm test
```

---
