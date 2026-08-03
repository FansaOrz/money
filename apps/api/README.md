# money-api

个人基金仪表盘 MVP 的后端 API 服务，基于 FastAPI + SQLAlchemy 2.0。

## 功能概览

- 数据模型：导入批次（imports）、账户（accounts）、基金标的（instruments）、交易流水（transactions）、持仓（positions）、组合快照（portfolio_snapshots）
- 组合汇总与持仓列表 API（当前基于持仓静态汇总，未含行情净值）
- 健康检查 `/api/health`
- 启动时自动建表（`create_all`），开发零迁移成本

> 说明：PDF 对账单解析器暂未实现，imports 表仅作为后续解析功能的落库结构预留。

## 快速开始

```bash
cd apps/api
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

默认使用 SQLite（`sqlite:///./money.db`），无需任何外部服务。

访问 <http://127.0.0.1:8000/docs> 查看交互式 API 文档。

## 配置

通过环境变量或 `.env` 文件配置（前缀 `MONEY_`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MONEY_DATABASE_URL` | `sqlite:///./money.db` | 数据库连接串；PostgreSQL 示例：`postgresql+psycopg://user:pass@localhost:5432/money` |
| `MONEY_CORS_ORIGINS` | `http://localhost:5173` | 允许的前端来源，逗号分隔 |
| `MONEY_AUTO_CREATE_TABLES` | `true` | 启动时是否自动创建数据表 |

切换到 PostgreSQL 时先安装驱动：`pip install ".[postgres]"`。

## 运行测试

```bash
cd apps/api
pytest
```

测试使用独立的临时 SQLite 数据库，不会污染开发数据。

## 目录结构

```
apps/api/
├── app/
│   ├── main.py            # FastAPI 应用入口（CORS、路由、启动建表）
│   ├── config.py          # pydantic-settings 配置
│   ├── db/
│   │   ├── base.py        # Declarative Base
│   │   └── session.py     # Engine 与 Session 管理
│   ├── models/            # SQLAlchemy ORM 模型
│   ├── schemas/           # Pydantic 请求/响应模型
│   ├── api/routes/        # 路由：health、portfolio
│   └── services/          # 业务服务：portfolio 汇总
└── tests/                 # pytest 测试
```

## 金额精度约定

所有金额/份额字段使用 `Decimal`，数据库层为 `NUMERIC`：

- 金额：`Numeric(18, 2)`
- 份额/净值：`Numeric(18, 4)`

SQLite 会将 NUMERIC 存为浮点，开发环境可接受；生产请使用 PostgreSQL 以保证精确十进制运算。API 响应中 Decimal 序列化为字符串，避免 JSON 浮点精度丢失。
