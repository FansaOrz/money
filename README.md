# Money · 本地投资研究工作台

Money 是一个面向个人使用的本地投资管理与研究系统。它从支付宝基金资产证明、交易明细 PDF 建立个人资产账本，并在此基础上提供基金发现、量化分析、研究信号、A 股研究、研究组合、模拟交易和定时数据同步。

系统默认在本机运行，业务数据存入 SQLite，研究数据使用 DuckDB + Parquet。所有分析、信号和模拟交易仅供研究参考，不构成投资建议。

能力状态统一使用四种术语：`已实现框架` 表示代码路径存在；`已用于正式版本`
表示已进入冻结策略证据；`已通过统计验收` 表示满足预注册样本外门禁；
`已完成生产演练` 表示在 PostgreSQL、券商沙箱、告警和灾备环境中实际演练。
当前版本 7 只属于运行链路验证，主动收益与统计门禁失败，既未通过投资有效性
验收，也未完成真实资金生产演练。第二轮边界和逐项证据见
[`docs/股票量化平台第二轮对抗性审查TODO清单.md`](docs/股票量化平台第二轮对抗性审查TODO清单.md)。

## 主要功能

### 资产管理

- 支付宝基金资产证明、基金交易明细 PDF 预览与两阶段导入
- 文件格式校验、异常提示和重复导入保护
- 总资产、净投入、估算盈亏及收益走势
- 基金持仓、交易流水、基金详情和底层持仓披露

### 基金研究

- 从全市场基金目录构建候选池
- 基金筛选、因子排名、双动量与风险分析
- 将均线、动量、回撤等指标翻译成白话趋势和“加仓 / 持有 / 观望 / 减仓”建议
- 后台聚合重复新闻，按基金自身、跟踪市场、行业和重仓股计算消息影响
- 将历史趋势、新闻环境和个人持仓占比合成为直白建议；页面不实时调用大模型
- 单基金及组合量化指标、历史策略回测
- 研究信号、候选池快照和研究组合
- 本地缓存、页面预热及返回位置记忆，减少重复加载和页面跳转等待

### 股票研究

- A 股股票池、行业和关键词筛选
- 个股行情、财务估值、研究因子和信号
- 面向非专业用户的白话趋势解读，专业技术指标默认折叠
- Point-in-time 数据口径与研究质量检查
- DuckDB + 按年份分区的 Parquet 研究数据仓库

### 跟踪与验证

- 基金及股票自选列表
- 基金、股票研究组合
- 100 万虚拟资金模拟盘
- A 股规则多因子两个月前向模拟：T 日信号、T+1 开盘成交、独立股票账本
- 市场指数、每日资讯和同步状态展示
- 多因子、风险模型、组合优化、HRP、走步回测及统计验证

### A 股规则量化

`/stock-quant` 是股票规则策略的前向验证工作台，默认策略为：

- 沪深300 + 中证500当前成分中数据就绪成员，账户创建时冻结候选池；
- 动态剔除 ST、停牌、次新、低流动性和历史样本不足股票；
- 质量 30%（ROE/ROA、现金流质量、应计、盈利稳定性等）、价值 25%
  （EP/BP/SP、股息率、FCF 收益率）、动量 20%（12-1/6-1/反转/残差）、
  趋势 15%、低风险 10%（60/120 日波动、Beta、残差波动和回撤）；
- 行业内缩尾与标准化；行业基准按自由流通市值，约束单股、行业、市值、
  Beta、流动性和 ADV 容量，并报告偏离、集中度、压力损失及现金；
- 月频调仓，T 日收盘生成信号，下一交易日开盘成交；
- 模拟佣金、最低 5 元佣金、卖出印花税、波动/参与率动态滑点、部分成交、
  现金应收、分红送转、配股、换股/代码变更、退市清算和各板块真实
  涨跌停/停牌；
- 信号、全候选因子快照、未成交订单、成交、持仓、每日净值和等权买入持有
  基准全部落库，可追溯到代码、数据清单和策略版本。

系统只有在沪深300+中证500全部当前成分的近期日线、行业、财务和 PE/PB 估值逐只
就绪，并且系统完成 purged walk-forward、验证集和一次性完全留出评估后，
才会把新版本从 `research` 晋级到 `validated`、`paper` 并创建空观察账户；
不能通过手工填写指标绕过冻结证据哈希。正式留出评估还要求 Git 工作区无
未提交改动，并固化代码提交、候选池、原始数据清单和验证结果哈希。账户首日只固化当日信号，不会当日
成交；下一真实行情日才模拟建仓。同一行情日重复运行幂等，不重复记账。观察满两个月后，
应同时检查超额收益、最大回撤、夏普、信息比率、换手和费用，不能只看收益。

当前正式前向版本 7 已绑定代码提交 `c823d3f` 和最终数据清单。历史验证区间为
2020-01-01～2026-07-31，三折走步、验证集和一次性留出集的最低数据覆盖率为
99.376%；留出集 319 个交易日收益 9.55%、Sharpe 0.472、最大回撤 -17.48%。
空账户 4 从 2026-08-04 开始、2026-10-04 结束，首日策略和基准净值均为 1.0，
首期 30 只目标信号等待 2026-08-06 开盘模拟执行。历史结果只用于检查策略和
工程链路，不能视为收益保证。

主要接口：

- `GET /api/stocks/paper/summary`：数据就绪度、观察进度、持仓、净值和指标
- `POST /api/stocks/paper/prepare`：历史走步/完全留出验证、冻结版本并创建空账户；
  显式传入 `create_new_version: true` 时从当前版本派生全新的研究版本，旧版本
  的验证证据不会被覆盖
- `POST /api/stocks/paper/run`：手动推进到最新真实行情日
- `GET /api/stocks/paper/trades`：模拟成交明细
- `POST /api/stocks/sync/market-close`：收盘全市场快照快速通道
- `POST /api/stocks/research/backtest`：历史月频规则回测
- `POST /api/stocks/research/backtest/walk-forward`：purged 走步选参、验证集
  与一次性完全留出评估
- `GET /api/health/deep`、`GET /api/metrics`：数据库、研究仓库、数据新鲜度、
  调度器和前向账户的健康与结构化指标

股票数据采用多源分工而非混合口径：中证接口维护指数成分，新浪历史日线为
深度主源、东方财富为历史回退；收盘快照优先东方财富、失败自动回退新浪；
东方财富/巨潮维护公开行业主源与回退，StockToday 可用时以申万 2021 一级行业
作为规则策略分组；东方财富/新浪/同花顺财务指标配合披露日期做 point-in-time
对齐；百度提供历史估值，腾讯收盘快照批量维护每日 PE(TTM)，PB 由同日收盘价
与最新已披露每股净资产计算。实际采用来源和同步错误会
写入数据状态，页面会在覆盖低于安全门槛时阻止启动模拟。

付费数据窗口可用时，`scripts/download_stocktoday_snapshot.py` 会通过
StockToday 的 Tushare 兼容接口补充原始数据仓库。任务按股票和接口分区保存
Parquet，使用无 token 的 JSONL 清单记录来源、参数、字段、行数和采集时间，
已完成分区会自动跳过，因此中断后可直接续传。目前下载范围包括：

- 沪深300、中证500自 2010 年以来的指数日线、权重和真实历史成分；
- 当前成分及历史快照出现过的股票的复权因子、估值、涨跌停、停牌和曾用名；
- 1861 只历史证券的原始日线、三大财务报表和月末横截面估值；退市证券
  使用 `stock_basic` 正式退市日期，合并/更名事件使用归档公告补录；
- 申万 2021 三级行业成员关系及同花顺行业目录；
- 财务指标、利润表、资产负债表、现金流量表和分红记录。

历史权重会物化为 `stock_universe_snapshots`，并推导
`index_membership_events` 调入调出事件。历史 PE(TTM)/PB 会物化到估值表，
财务指标按公告日建立 point-in-time 口径，带日期的曾用名记录用于历史 ST
过滤；规则策略行业中性分组优先使用申万 2021 一级行业。历史回测按每个
月末信号日读取当时股票池，并按信号日读取估值；任一期核心数据覆盖低于
门槛会拒绝运行，不会静默缩小股票池或回退当前成分。原始宽表仍完整保留在
Parquet 中，`trade_cal`、`stk_limit` 和 `suspend_d` 已用于交易日及执行判断。

续传数据快照（凭据只允许通过当前进程环境注入，不写入文件、命令参数或采集清单）：

```bash
cd apps/api
read -rsp "Tushare token: " TUSHARE_TOKEN && export TUSHARE_TOKEN
echo
python scripts/download_stocktoday_snapshot.py \
  --skill-dir ../../tmp/agent_skill_tushare \
  --database ../../data/money.db \
  --output-dir ../../data/research/tushare_snapshot \
  --start-date 20100101 \
  --universe-source historical

MONEY_DATABASE_URL=sqlite:///../../data/money.db \
python scripts/import_stocktoday_snapshot.py \
  --snapshot-dir ../../data/research/tushare_snapshot

python scripts/validate_stocktoday_snapshot.py \
  --database ../../data/money.db \
  --snapshot-dir ../../data/research/tushare_snapshot
```

三大报表和执行原始表可按研究需要幂等导入 `quant_data_records` 规范化
PIT 层；高频日线仍直接读取不可变 Parquet，避免把海量行情重复写入业务库。每个
字段保存来源、原值、规范值和质量状态，原始 Parquet 永不改写：

```bash
cd apps/api
PYTHONPATH=. python -c \
  "from pathlib import Path; from app.db.session import SessionLocal; \
from app.services.quant_data_governance import import_tushare_snapshot; \
db=SessionLocal(); print(import_tushare_snapshot(db, Path('../../data/research'))); db.close()"
```

## 技术栈

- Web：Next.js 16、React 19、TypeScript、Tailwind CSS 4
- API：FastAPI、SQLAlchemy、Pydantic
- 业务数据库：SQLite（本机默认）或 PostgreSQL
- 研究仓库：DuckDB、Parquet、PyArrow
- 数据处理：Pandas、AkShare、Requests

目录结构：

```text
.
├── apps/
│   ├── api/                 # FastAPI API、同步任务、量化及研究服务
│   └── web/                 # Next.js Web 应用
├── data/                    # 本机数据库、研究仓库、日志和 PID
│   └── research/            # DuckDB 与 Parquet 分区数据
├── infra/docker-compose.yml
├── tmp/                     # 待导入 PDF（包含敏感信息，请勿提交）
├── start.sh
├── stop.sh
└── sync_navs.sh
```

## 本机运行

### 环境要求

- Python 3.11+
- Node.js 20+ 与 npm
- 可访问所用公开数据源的网络环境

首次安装依赖：

```bash
cd apps/api
python -m pip install -e ".[dev]"

cd ../web
npm ci

cd ../..
```

启动全部服务：

```bash
bash start.sh
```

访问地址：

- Web：<http://localhost:3000>
- API：<http://localhost:8001>
- OpenAPI 文档：<http://localhost:8001/docs>
- 健康检查：<http://localhost:8001/api/health>

停止服务：

```bash
bash stop.sh
```

`start.sh` 会先执行 `alembic upgrade head`，再启动 API、生产模式 Web 和常驻
调度器；服务进程关闭自动建表，数据库结构只由迁移推进。若
`data/money.db` 不存在，它还会通过迁移建库、导入 `tmp/` 下可识别的 PDF，
并同步基金历史净值和最新净值。前端尚未构建时会自动执行 `npm run build`。

> 自动初始化脚本目前按项目路径 `/root/Src/money` 查找 `tmp/`。若项目放在其他位置，请通过 Web 的“数据导入”页面导入 PDF，或先调整 `apps/api/app/services/bootstrap.py` 中的路径。

### 运行文件

进程 PID：

- `data/api.pid`
- `data/web.pid`
- `data/scheduler.pid`

日志：

- `data/api.log`
- `data/web.log`
- `data/scheduler.log`
- `data/sync.log`

## 数据导入

推荐在 Web 的“数据导入”页面操作：

1. 上传基金交易明细 PDF，检查预览后确认。
2. 上传基金资产证明 PDF，核对总资产和持仓数量后确认。
3. 在总览、持仓和交易记录页面检查导入结果及异常告警。

预览结果只在后端内存中临时保留，默认有效期为 30 分钟；只有确认后才会写入数据库。重复上传同一文件不会重复写入持仓或交易。

## 数据同步与调度

常驻调度器由 `start.sh` 启动，所有计划时间均为北京时间（Asia/Shanghai）：

| 任务 | 计划时间 |
| --- | --- |
| 每日资讯 | 每小时第 17 分 |
| 候选池历史净值回填 | 每日 03:23 |
| 美股指数 | 每日 07:30 |
| A 股日线 | 每日 17:05 |
| A 股行业/财务/估值覆盖补齐 | 每日 16:10 |
| A 股规则策略前向模拟 | 每日 18:30 |
| A 股及港股市场指数 | 每日 17:30 |
| 持仓基金净值与组合快照 | 每日 19:30、20:30、22:00 |
| 模拟盘 | 每日 20:30 |
| 基金底层持仓披露检查 | 每月 1 日 19:05 |
| 全市场基金目录 | 每周日 02:30 |
| 数据库/Parquet/策略账本备份与恢复校验 | 每周日 04:00 |

调度任务同时写入 `persistent_jobs`，包含租约锁、依赖、重试和检查点；进程
重启后会恢复过期任务。失败任务和数据质量问题通过 `/api/sync/status` 的
`alerts` 返回，并显示在页面顶部状态栏。

数据库结构由 Alembic 管理。开发测试仍可使用 `create_all` 便利建库，生产环境
设置 `MONEY_ENVIRONMENT=production` 后会禁止该路径，部署前必须执行：

```bash
cd apps/api
MONEY_DATABASE_URL=postgresql+psycopg://... alembic upgrade head
```

备份与恢复演练只恢复到不存在的新目录，禁止覆盖现有数据：

```bash
cd apps/api
python -m app.services.backup_job create ../../data/backups/manual-YYYYMMDD
python -m app.services.backup_job verify ../../data/backups/manual-YYYYMMDD
python -m app.services.backup_job restore ../../data/backups/manual-YYYYMMDD /tmp/money-restore-check
```

如需手动增量同步基金最新净值：

```bash
bash sync_navs.sh
```

该命令只更新当前持仓基金；全市场候选池数据由凌晨低优先级任务分批维护。

按批次回填基金近五年历史净值：

```bash
cd apps/api
MONEY_DATABASE_URL="sqlite:///../../data/money.db" \
python -m app.services.sync_backfill_job --batch-size 20 --batch 0
```

## 配置

后端配置使用 `MONEY_` 环境变量前缀：

本机通过 `start.sh` 运行时，可把这些配置写入 `apps/api/.env`；根目录
`.env` 主要供 Docker Compose 使用。真实 API Key 不要提交到 Git。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MONEY_DATABASE_URL` | `sqlite:///./money.db` | SQLAlchemy 数据库连接串 |
| `MONEY_CORS_ORIGINS` | localhost:3000 | 允许的前端来源，支持逗号分隔 |
| `MONEY_IMPORT_SESSION_TTL_MINUTES` | `30` | 导入预览有效期 |
| `MONEY_AUTO_CREATE_TABLES` | `true` | API 启动时自动建表 |
| `MONEY_RESEARCH_DATA_DIR` | `./data/research` | Parquet 研究数据目录 |
| `MONEY_RESEARCH_DB` | `./data/research/research.duckdb` | DuckDB 文件路径 |
| `MONEY_RESEARCH_SYNC_BATCH_SIZE` | `200` | A 股日线单批同步数量 |
| `MONEY_SCHEDULED_STOCK_SYNC_BATCH_SIZE` | `40` | 调度器后台 A 股日线单批数量 |
| `MONEY_SCHEDULED_STOCK_SYNC_TIMEOUT_MINUTES` | `60` | A 股后台任务最长运行分钟数，超时后从断点续跑 |
| `MONEY_SCHEDULED_STOCK_REFERENCE_BATCH_SIZE` | `20` | 当前指数成分行业/财务/估值缺口的每日补齐批次 |
| `MONEY_NEWS_ANALYSIS_ENABLED` | `true` | 后台聚合并分析新资讯 |
| `MONEY_NEWS_ANALYSIS_LOOKBACK_DAYS` | `30` | 新闻事件分析回看天数 |
| `MONEY_NEWS_ANALYSIS_BATCH_SIZE` | `100` | 每轮最多处理的新资讯数 |
| `MONEY_NEWS_LLM_ENABLED` | `false` | 是否使用 OpenAI-compatible 模型分析新闻 |
| `MONEY_NEWS_LLM_BASE_URL` | OpenAI API 地址 | 模型服务的 `/v1` 根地址，也可填写兼容服务 |
| `MONEY_NEWS_LLM_API_KEY` | 空 | 模型服务密钥；不要提交真实密钥 |
| `MONEY_NEWS_LLM_MODEL` | 空 | 新闻分析使用的模型名；留空时使用规则初判 |
| `MONEY_NEWS_LLM_TIMEOUT_SECONDS` | `30` | 后台单次模型请求超时秒数 |
| `NEXT_PUBLIC_API_URL` | 空 | 浏览器 API 地址；服务器部署应留空，使用同源代理 |
| `API_PROXY_URL` | `http://127.0.0.1:8001` | Next.js 服务端代理到 FastAPI 的地址 |

本机 `start.sh` 会显式把主数据库设为项目根目录下的 `data/money.db`。如果手动从 `apps/api` 启动 API，建议同时显式设置数据库和研究仓库路径：

```bash
cd apps/api
MONEY_DATABASE_URL="sqlite:///../../data/money.db" \
MONEY_RESEARCH_DATA_DIR="../../data/research" \
MONEY_RESEARCH_DB="../../data/research/research.duckdb" \
uvicorn app.main:app --reload --port 8001
```

另开终端启动前端开发服务器：

```bash
cd apps/web
NEXT_PUBLIC_API_URL= API_PROXY_URL=http://127.0.0.1:8001 npm run dev
```

新闻分析由调度器在每小时资讯同步后执行。不开启大模型时系统仍会用保守规则生成
“规则初判”，并降低建议可信度；开启后，模型只负责把新闻整理成固定 JSON，
最终评分、基金持仓映射和加减仓约束仍由本地程序计算。也可手动触发：

```bash
curl -X POST http://127.0.0.1:8001/api/news/analyze
```

## Docker Compose

仓库保留了 PostgreSQL + API + Web 的 Compose 配置：

```bash
cp .env.example .env
make up
make logs
make down
```

Docker 模式的 API 端口为 `8000`，Web 端口为 `3000`。

Compose 已为 PostgreSQL 和研究数据仓库配置持久卷，并通过 API 健康检查控制 Web 启动顺序。

## 验证

后端测试：

```bash
cd apps/api
python -m pytest
```

前端检查：

```bash
cd apps/web
npm run typecheck
npm run build
```

也可以从项目根目录运行：

```bash
make test
```

`make lint` 会依次运行后端 Ruff 和前端 TypeScript 类型检查；GitHub Actions 也会在推送和 Pull Request 时执行后端测试及前端构建。

## 隐私与数据安全

- 上传的 PDF 仅用于后端解析，数据库不保存 PDF 原文件。
- 数据库不保存姓名或身份证号，日志不会主动输出完整基金账户和订单号。
- `tmp/`、PDF、数据库和本机运行数据均不应提交到公开仓库。
- `data/` 中的数据库与研究仓库可能包含个人持仓和研究结果，备份或迁移时请妥善保管。
- API Key、Tushare token 与券商凭据只允许通过环境变量/本机忽略文件读取；
  生产环境强制 admin/readonly 两级 API Key 并记录修改操作审计。
- OMS/RMS 默认且目前只启用 `simulated` 适配器，含订单、成交、撤单、资金、
  持仓、对账、重复下单保护和紧急停止；未安装并配置券商适配器时不会产生
  任何真实下单副作用。
