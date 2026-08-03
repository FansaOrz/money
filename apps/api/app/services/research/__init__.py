"""A 股研究数据层（research repository）。

模块划分：
- ak_fetch：AKShare 调用的薄封装，所有网络失败在此降级为 None，便于 mock 测试；
- parquet_store：raw 日线 Parquet 数据湖（读/增量写/断点）；
- stock_data：master + 日线同步 + coverage 状态；
- stock_universe：指数当前成分 + 成分调整事件 CSV 导入 + 快照回放；
- stock_fundamentals：财务指标、披露日程、估值、历史名称/ST。
"""
