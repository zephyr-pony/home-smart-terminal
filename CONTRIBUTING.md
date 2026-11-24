# 参与开发

这个项目是家庭场景的语音助手，数据处理偏隐私、行为偏保守：宁可多问一句，也不猜。
改动请优先保证「写进去的和念出来的一致」，其次才是快。

更细的架构约定在 [AGENTS.md](AGENTS.md)，规划与待决策在 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 本地跑起来

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt        # CLI/语音需要；只做网页可看下面的提示
cp .env.example .env
```

最简 `.env` 只需要三行（其余都有默认值）：

```dotenv
LLM_BASE_URL=https://你的网关/v1
LLM_API_KEY=sk-...
LLM_MODEL=你的模型名
```

```bash
python server.py      # http://127.0.0.1:8000
python main.py        # CLI
python main.py wake   # 唤醒词常驻
```

只想调试文字/网页、不想下 900MB 语音模型：在 `.env` 里设 `STT_ENABLED=0`，
`/api/voice` 会返回 503，其它功能不受影响。Linux 上 `pip install pyaudio` 需要先装
`portaudio19-dev`；只做网页开发可以不装 `pyaudio` / `pygame`。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

- 单测不需要 `.env`，也不会碰 `data/`（都用 `:memory:` 或临时目录）。
- 正确率评测打真模型，慢且有网络依赖，按需手动跑：
  ```bash
  python scripts/eval_accuracy.py --only 提醒
  ```
- 提交前至少跑一遍 `pytest tests -q` 和 `python -m py_compile *.py tests/*.py`。
- CI 只装 `requirements-ci.txt`（不含 torch/funasr/pyaudio）；如果你给服务端或适配器
  加了顶层 import，记得同步这个文件。

## 代码约定（摘要）

- Python 3.9+，不要用 `X | None`、`match` 这类 3.10+ 语法。
- 领域层（`domain/`）不 import LLM、FastAPI、音频或渠道 SDK。
- 写操作不直接落表：走 `ProposalService`；高风险动作先确认。
- 口播文案统一走 `agent/wording.py`，日期不要自己拼。
- 工具 `execute` 必须返回字符串（直接进 LLM 上下文）。
- 注释/docstring 用中文，解释「为什么」，不是复述代码。
- 新增配置项要同时改 `.env.example`、`config.py`，README 配置表；能被前端改的再加
  `server.CONFIG_FIELDS`。

## 加一个工具

1. `tools.py` 用 `_build(...)` 注册 `schema` + `execute`，`assistant` 会自动聚合。
2. 参数名和 JSON Schema 对齐；写操作走 proposal，删除/付款走确认。
3. 补测试：工具本身、agent 选得对不对、失败怎么降级。
4. 如果是查询类且可能为空，考虑加入 `wording.EMPTY_QUERY_REPLY` 那条短接。

## 不要提交

- `.env`、`data/`、`backups/`、音频文件、任何密钥或真实家庭数据。
- 日志里不要贴 MCP / API token（访问日志已对 `token=` 做脱敏，别把它关掉）。

## PR

- 说清楚「用户原来会遇到什么、现在怎么变」，有评测或真机结果就贴数字。
- 行为变化（尤其写操作、提醒、口播）请在 `docs/changes-2026-11.md` 加一节。
- 大的方向变化先开 issue 或在 `ROADMAP` 的「待决策」里记一笔，别直接改代码。
