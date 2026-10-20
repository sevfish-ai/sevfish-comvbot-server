"""
表格（自定义数据表）数据模型

每张用户表格 = 一张真实 MySQL 表（t_tbl_{id}），字段定义存元数据表。
"""
from typing import Any, Dict, List
from pydantic import BaseModel, Field

# 支持的字段类型（映射到 MySQL DDL）
FIELD_TYPES = ["varchar", "int", "float", "datetime", "text", "json"]


class FieldDef(BaseModel):
    """字段定义"""

    name: str = Field(
        ..., pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$", description="字段名（字母开头，仅字母数字下划线）"
    )
    type: str = Field(default="varchar", description="字段类型：varchar/int/float/datetime/text/json")
    label: str = Field(default="", description="字段中文名")
    required: bool = Field(default=False, description="是否必填")


class TableCreateRequest(BaseModel):
    """创建表格请求模型"""

    name: str = Field(..., min_length=1, max_length=100, description="表格名称")
    description: str = Field(default="", max_length=500, description="描述")
    fields: List[Dict[str, Any]] = Field(
        default_factory=list, description="字段定义列表：[{name,type,label,required}]"
    )


class TableGrantRequest(BaseModel):
    """授权智能体访问表格请求模型"""

    agent_id: int = Field(..., description="智能体 ID")
    permission: int = Field(default=0, ge=0, le=1, description="权限：0=只读 1=读写")


class TableInfo(BaseModel):
    """表格信息模型"""

    id: int = Field(..., description="表格 ID")
    name: str = Field(..., description="表格名称")
    table_key: str = Field(default="", description="物理表标识（tbl_{id}）")
    description: str = Field(default="", description="描述")
    field_count: int = Field(default=0, description="字段数")
    row_count: int = Field(default=0, description="数据行数")
    grant_count: int = Field(default=0, description="授权智能体数")
    created_at: str = Field(default="", description="创建时间")
