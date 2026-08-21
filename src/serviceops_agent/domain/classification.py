"""意图分类使用的结构化领域模型。

该模型位于 domain 层，不依赖具体模型服务商。无论使用哪一种 LLM，输出都必须先通过
Pydantic 校验，才能写入 LangGraph State 并参与条件路由。
"""

# BaseModel 提供运行时数据校验；Field 为模型字段增加约束和 JSON Schema 描述。
from pydantic import BaseModel, Field

# Intent 是下游路由允许处理的有限枚举，模型不能返回集合以外的任意意图。
from serviceops_agent.domain.enums import Intent


class IntentClassification(BaseModel):
    """模型对一条售后请求给出的可验证分类结果。"""

    # intent 决定 LangGraph 下一跳，只允许 FAQ、订单状态或人工接管三种结果。
    intent: Intent = Field(description="售后请求所属的有限业务意图")
    # confidence 表示模型对本次分类的把握，低于系统阈值时会被覆盖为人工接管。
    confidence: float = Field(ge=0.0, le=1.0, description="0 到 1 之间的分类置信度")
    # reason 只保存简短分类依据，用于审计和错误分析，不要求模型输出详细思维过程。
    reason: str = Field(min_length=1, max_length=200, description="简短、可审计的分类依据")
