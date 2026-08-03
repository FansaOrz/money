"""研究数据仓库（Research Warehouse）。

基于 DuckDB + Parquet 分区目录的本地研究数据层，与业务库（SQLAlchemy/SQLite/PG）完全隔离，
用于回测/研究场景的可复现数据访问：

- ``warehouse``   DuckDB 连接管理、Parquet 分区目录初始化、建表 DDL；
- ``snapshots``   统一快照元数据列（effective_date / available_at / ingested_at / source / row_hash）；
- ``repository``  ``MarketDataRepository`` 抽象与 ``DuckDBRepository`` / ``CompositeRepository`` 实现；
- ``quality``     数据质量检查（主键重复、频率缺口、非正价格、财报未来日期）。

五张核心数据集：
``fund_nav``（基金净值）、``stock_daily``（股票日线）、``universe_membership``（宇宙成分）、
``fundamentals``（财务）、``factor_panel``（因子面板）。

本包不依赖 FastAPI 路由与业务 Service，可独立用于研究脚本。
"""
