"""通用 Schema 配置与类型。

所有响应模型统一使用 ConfiguredBaseModel：
- Decimal 序列化为字符串，避免 JSON 浮点精度丢失；
- 支持从 ORM 对象直接构造（from_attributes）。
"""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer

# 金额/份额在 API 层以字符串形式输出
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v, "f"), return_type=str, when_used="json"),
]


class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
