# Money · 本地投资研究工作台

Money 是一个面向个人使用的本地投资管理与研究系统。它从支付宝基金资产证明、交易明细 PDF 建立个人资产账本，并在此基础上提供基金发现、量化分析、研究信号、A 股研究、研究组合、模拟交易和定时数据同步。

系统默认在本机运行，业务数据存入 SQLite，研究数据使用 DuckDB + Parquet。所有分析、信号和模拟交易仅供研究参考，不构成投资建议。

## 主要功能

### 资产管理

- 支付宝基金资产证明、基金交易明细 PDF 预览与两阶段导入
- 文件格式校验、异常提示和重复导入保护
- 总资产、净投入、估算盈亏及收益走势
- 基金持仓、交易流水、基金详情和底层持仓披露

### 基金研究

- 从全市场基金目录构建候选池
- 基金筛选、因子排名、双动量与风险分析
- 单基金及组合量化指标、历史策略回测
- 研究信号、候选池快照和研究组合

### 股票研究

- A 股股票池、行业和关键词筛选
- 个股行情、财务估值、研究因子和信号
- Point-in-time 数据口径与研究质量检查
- DuckDB + 按年份分区的 Parquet 研究数据仓库

### 跟踪与验证

- 基金及股票自选列表
- 基金、股票研究组合
- 100 万虚拟资金模拟盘
- 市场指数、每日资讯和同步状态展示
- 多因子、风险模型、组合优化、HRP、走步回测及统计验证

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

`start.sh` 会启动 API、生产模式 Web 和常驻调度器。若 `data/money.db` 不存在，它还会自动建库、导入 `tmp/` 下可识别的 PDF，并同步基金历史净值和最新净值。前端尚未构建时会自动执行 `npm run build`。

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
| A 股及港股市场指数 | 每日 17:30 |
| 基金净值、组合快照与模拟盘 | 每日 20:30 |
| 基金底层持仓披露检查 | 每月 1 日 19:05 |
| 全市场基金目录 | 每周日 02:30 |

调度器启动时也会按需同步基金净值，并立即同步资讯和市场指数。任务结果写入 `sync_runs`，可通过 `/api/sync/status` 或页面顶部状态栏查看。

如需手动增量同步基金最新净值：

```bash
bash sync_navs.sh
```

按批次回填基金近五年历史净值：

```bash
cd apps/api
MONEY_DATABASE_URL="sqlite:///../../data/money.db" \
python -m app.services.sync_backfill_job --batch-size 20 --batch 0
```

## 配置

后端配置使用 `MONEY_` 环境变量前缀：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MONEY_DATABASE_URL` | `sqlite:///./money.db` | SQLAlchemy 数据库连接串 |
| `MONEY_CORS_ORIGINS` | localhost:3000 | 允许的前端来源，支持逗号分隔 |
| `MONEY_IMPORT_SESSION_TTL_MINUTES` | `30` | 导入预览有效期 |
| `MONEY_AUTO_CREATE_TABLES` | `true` | API 启动时自动建表 |
| `MONEY_RESEARCH_DATA_DIR` | `./data/research` | Parquet 研究数据目录 |
| `MONEY_RESEARCH_DB` | `./data/research/research.duckdb` | DuckDB 文件路径 |
| `MONEY_RESEARCH_SYNC_BATCH_SIZE` | `200` | A 股日线单批同步数量 |
| `NEXT_PUBLIC_API_URL` | 视启动方式而定 | Web 请求的 API 地址 |

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
NEXT_PUBLIC_API_URL=http://127.0.0.1:8001 npm run dev
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

> 当前后端只读取带 `MONEY_` 前缀的配置，而现有 Compose 文件中的部分变量仍使用无前缀名称；研究数据目录也未挂载持久卷。因此 Compose 更适合开发验证，正式使用前应统一环境变量名称并补充数据卷。

## 验证

后端测试：

```bash
cd apps/api
python -m pytest
```

如需运行 Ruff，请先安装它（当前未包含在项目的 dev 依赖中）：

```bash
python -m pip install ruff
python -m ruff check apps/api
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

当前 `Makefile` 的 `lint` 目标仍调用前端未定义的 `npm run lint`，请暂时使用 `npm run typecheck`。

## 隐私与数据安全

- 上传的 PDF 仅用于后端解析，数据库不保存 PDF 原文件。
- 数据库不保存姓名或身份证号，日志不会主动输出完整基金账户和订单号。
- `tmp/`、PDF、数据库和本机运行数据均不应提交到公开仓库。
- `data/` 中的数据库与研究仓库可能包含个人持仓和研究结果，备份或迁移时请妥善保管。
- 模拟盘只产生虚拟记录，不涉及真实资金、真实下单或交易指令。
