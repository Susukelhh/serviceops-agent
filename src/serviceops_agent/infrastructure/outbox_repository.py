"""事务 Outbox 扫描与状态推进使用的最小仓库协议。"""

# datetime 允许测试或调度器明确指定扫描时点，避免依赖不稳定的等待。
from datetime import datetime

# Protocol 让内存和 SQLite 退货仓库以结构化类型同时实现本协议。
from typing import Protocol

# 强类型记录与有限状态保证协调器不处理任意 JSON 字典。
from serviceops_agent.domain.outbox import OutboxEventRecord, OutboxStatus


class ReturnOutboxRepository(Protocol):
    """协调器依赖的最小 Outbox 读取与状态变更边界。"""

    def list_pending(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[OutboxEventRecord]:
        """按创建顺序返回已经到达处理时间的待投递事件。"""

    def mark_processed(self, event_id: str) -> OutboxEventRecord:
        """幂等地把事件标记为已处理，并返回最新状态。"""

    def record_failure(
        self,
        event_id: str,
        *,
        error_code: str,
        max_attempts: int = 3,
    ) -> OutboxEventRecord:
        """记录有限错误码、计算退避时间，并在达到上限时转为死信。"""

    def get_outbox_event(self, event_id: str) -> OutboxEventRecord | None:
        """按稳定事件 ID 查询当前快照。"""

    def count_outbox(self, status: OutboxStatus | None = None) -> int:
        """统计全部或指定状态的事件，供 readiness、测试和演示使用。"""
