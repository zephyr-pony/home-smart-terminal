# 项目当前状态（2026-08-18）

## 已完成（均已提交推送 GitHub）
- ✅ 阶段 1 MVP：本地语音交互 + 记忆系统（aa5be7f）
- ✅ 阶段 2：FastAPI 服务端 + Web 客户端（316175c），手机/平板访问
- ✅ 3 个 bug 修复：
  - 时间戳混淆（相对时间转绝对日期）→ 6c2313d
  - memory 参数错位（FTS 索引用摘要）→ 6c2313d
  - 前端欢迎语误导 → 81cf509
  - gitignore playwright 快照 → 4eb5e85

## 进行中：阶段 3（agent 化 + 物品管理 + 到期提醒）
- ✅ 计划已写完并通过咨询（Metis 需求分析 + Oracle 架构评估 + Momus 审查）
- 📄 计划文档：`.sisyphus/plans/phase3-agent.md`
- ⏸️ 等待用户确认计划后开始实施

## 待办（实施阶段 3）
1. Tool 接口化（memory 4 件套 + get_current_date）
2. llm._chat_with_tools（暴露 tool_calls）+ process_text 改 agent loop
3. items 表 + 物品管理工具
4. APScheduler 到期提醒
5. 回归 + 提交推送

## 你接下来可以做什么
- 回复"确认" → 开始实施阶段 3
- 或先体验：运行 `start.bat`，浏览器开 http://127.0.0.1:8000