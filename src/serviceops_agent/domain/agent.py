"""受控 Agent 工具循环使用的稳定领域模型。"""

# StrEnum 让动作既受枚举约束，又能直接写入 JSON 和 LangGraph State。
from enum import StrEnum

# BaseModel 提供运行时校验；Field 声明约束；两个 validator 负责规范化与跨字段不变量。
from pydantic import BaseModel, Field, field_validator, model_validator


class AgentAction(StrEnum):
    """规划器只能选择的四种有限下一步动作。"""

    # CALL_TOOL 表示请求执行白名单中的一个工具。
    CALL_TOOL = "call_tool"
    # FINISH 表示已经取得足够观察结果，可以结束循环并组织回答。
    FINISH = "finish"
    # CLARIFY 表示缺少工具必需参数，应向用户追问而不是猜测。
    CLARIFY = "clarify"
    # HANDOFF 表示规划器认为当前任务不应继续自动执行，需要人工接管。
    HANDOFF = "handoff"


class ToolCallPlan(BaseModel):
    """规划器输出、尚未由执行器信任的一步结构化计划。"""

    # action 决定条件边的下一跳，不能输出集合外的任意动作。
    action: AgentAction = Field(description="本轮要执行的有限 Agent 动作")
    # tool_name 只有 CALL_TOOL 时存在，执行器还会进行独立白名单校验。
    tool_name: str | None = Field(default=None, max_length=100)
    # order_id 是当前唯一工具计划允许模型建议的业务参数；user_id 永远不在 Schema 中。
    order_id: str | None = Field(default=None, pattern=r"^SO\d{6}$")
    # reason 只保存简短决策依据，不要求模型输出详细思维过程。
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> object:
        """在正则校验前统一模型可能返回的小写前缀和两侧空格。"""

        # 只有字符串才执行规范化，其他类型交给 Pydantic 产生标准类型错误。
        if isinstance(value, str):
            # 与真实 Tool 的 OrderLookupInput 保持相同大小写和空白处理规则。
            return value.strip().upper()
        # 原样返回 None 或其他对象，让字段类型负责后续校验。
        return value

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ToolCallPlan":
        """保证调用动作与工具字段一致，避免含糊或夹带式计划。"""

        # CALL_TOOL 必须同时提供工具名和订单号，否则执行器没有明确动作。
        if self.action == AgentAction.CALL_TOOL:
            # 空名称或空订单号都视为无效结构化计划。
            if not self.tool_name or not self.order_id:
                # ValueError 会被 Pydantic 转换为结构化校验错误。
                raise ValueError("call_tool 动作必须提供 tool_name 和 order_id")
            # 合法工具调用无需继续检查非调用动作规则。
            return self
        # FINISH/CLARIFY/HANDOFF 不得夹带工具名或订单号，防止动作语义与负载冲突。
        if self.tool_name is not None or self.order_id is not None:
            # 调用方必须清空工具字段后重新生成计划。
            raise ValueError("非 call_tool 动作不能携带工具名或 order_id")
        # 返回完成跨字段校验的当前对象。
        return self

    def tool_arguments(self) -> dict[str, str]:
        """由服务端把扁平计划转换成实际 Tool 参数，而不是让模型提交任意字典。"""

        # 只有经过跨字段校验的调用动作才会同时拥有 order_id。
        if self.action == AgentAction.CALL_TOOL and self.order_id is not None:
            # 当前唯一允许参数就是订单号；可信 user_id 仍由工具闭包注入。
            return {"order_id": self.order_id}
        # 非调用动作没有工具参数。
        return {}


class ToolExecutionRecord(BaseModel):
    """一次工具调用尝试的可审计记录。"""

    # tool_name 是实际经过白名单检查的稳定工具名称。
    tool_name: str = Field(min_length=1, max_length=100)
    # arguments 保存经过计划 Schema 校验的参数，不包含系统绑定的 user_id。
    arguments: dict[str, str] = Field(max_length=10)
    # fingerprint 是工具名与规范参数的 SHA-256，用于本请求内重复调用检测。
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    # succeeded 表示工具调用和输出领域校验是否都成功。
    succeeded: bool
    # result 只在成功时保存经过领域 Schema 校验的结构化输出。
    result: dict[str, object] = Field(default_factory=dict)
    # error_code 是失败时的有限内部类别，不保存第三方异常正文。
    error_code: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_execution_outcome(self) -> "ToolExecutionRecord":
        """确保成功记录有结果，失败记录有错误码且不夹带不可信结果。"""

        # 成功调用必须保存已校验结果，并且不能同时声明错误码。
        if self.succeeded:
            # 缺少结果或存在错误码都会让审计记录自相矛盾。
            if not self.result or self.error_code is not None:
                # 在写入 State 前立即拒绝错误记录。
                raise ValueError("成功工具记录必须包含 result 且不能包含 error_code")
            # 成功分支已经满足全部不变量。
            return self
        # 失败调用必须提供有限错误码，并且不得保存可能未经校验的原始结果。
        if self.error_code is None or self.result:
            # 防止异常对象或半截工具响应混入后续规划上下文。
            raise ValueError("失败工具记录必须包含 error_code 且 result 必须为空")
        # 返回完成校验的失败记录。
        return self
