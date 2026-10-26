# 贡献 VideoForge

## 两条硬规矩（违反会被 CI 拦下）

1. **执行入口只有一个** —— `core.agent_tools_stages.run_stage`。
   不要在别处再写一套"怎么跑某一步"。
2. **不动业务数据** —— sidecar 只写 `tasks` 表；业务表的写入走既有 service。

底座（`deepseek-harness-master/`）的详细规范见其中的
`CONTRIBUTING.zh.md` 与 `AGENTS.md`。

## 提交前

```bash
# Python 侧
python -m py_compile backend/core/*.py backend/api/*.py
python -m pytest backend/tests -q

# 前端
node --check frontend/js/chat.js
```

## 花钱相关的改动

任何会调用**付费视频 API** 的路径，必须先过 `core/budget.py` 的闸门：
- 生成前 `check_budget()`（含每次候选重新核算）
- 生成后 `add_cost()` 累加
- 新增 provider 时，`core/pricing.py` 要补单价，**未知单价按最贵算**（宁可少生成，不要超支）
