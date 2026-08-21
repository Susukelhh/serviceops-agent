"""把完整知识文档切分为具有稳定标识和来源元数据的证据切片。"""

# uuid5 基于稳定输入生成可重复 UUID；NAMESPACE_URL 提供固定命名空间。
from uuid import NAMESPACE_URL, uuid5

# 领域文档和切片模型确保切分结果仍然携带版本、来源和生效日期。
from serviceops_agent.domain.knowledge import KnowledgeChunk, KnowledgeDocument


class KnowledgeChunker:
    """按字符窗口和自然边界切分中文知识文档。"""

    def __init__(self, *, chunk_size: int, chunk_overlap: int) -> None:
        """保存切片大小，并验证重叠不能覆盖整个窗口。"""

        # chunk_size 是每个切片允许包含的最大近似字符数。
        self._chunk_size = chunk_size
        # overlap 必须小于窗口，否则下一轮起点无法前进并会形成死循环。
        if chunk_overlap >= chunk_size:
            # 在应用启动或测试构建阶段立即暴露错误配置，而不是索引时无限循环。
            raise ValueError("rag_chunk_overlap 必须小于 rag_chunk_size")
        # 合法重叠长度用于相邻切片保留上下文连续性。
        self._chunk_overlap = chunk_overlap

    def split_documents(self, documents: list[KnowledgeDocument]) -> list[KnowledgeChunk]:
        """按输入文档顺序生成全部切片，保证建库结果可重复。"""

        # 结果列表保持“文档顺序 → 文档内切片顺序”，便于测试和人工抽查。
        chunks: list[KnowledgeChunk] = []
        # 每份文档独立切分，绝不把不同来源的正文拼进同一个证据切片。
        for document in documents:
            # extend 保留单文档方法生成的有序切片。
            chunks.extend(self.split_document(document))
        # 返回可直接向量化和写入 Qdrant 的领域对象列表。
        return chunks

    def split_document(self, document: KnowledgeDocument) -> list[KnowledgeChunk]:
        """优先在段落、换行和中文标点处截断一份文档。"""

        # strip 只移除正文首尾空白；文档仓库中的原始对象不会被修改。
        content = document.content.strip()
        # 空正文理论上已被 Pydantic 拦截，这个保护使算法面对全空白文本也能安全结束。
        if not content:
            # 返回空列表而不是创建无意义零向量切片。
            return []

        # chunks 收集当前文档的有序切片。
        chunks: list[KnowledgeChunk] = []
        # start 指向下一切片在正文中的零基起始位置。
        start = 0
        # chunk_index 为文档内稳定序号，参与 UUID 和检索元数据。
        chunk_index = 0

        # 每轮至少推进一个字符，直到覆盖完整正文。
        while start < len(content):
            # hard_end 是不超过 chunk_size 的最远截断位置。
            hard_end = min(start + self._chunk_size, len(content))
            # 优先选择自然语言边界，减少把同一句政策拆到两个切片的概率。
            end = self._choose_boundary(content, start=start, hard_end=hard_end)
            # 截取并清理切片边缘空白，内部段落格式保持不变。
            chunk_content = content[start:end].strip()

            # 只有包含真实文本时才创建领域切片。
            if chunk_content:
                # UUID 输入包含版本、位置和内容；政策更新后会产生新切片 ID。
                stable_key = (
                    f"{document.document_id}:{document.version}:{chunk_index}:"
                    f"{start}:{end}:{chunk_content}"
                )
                # uuid5 对相同 stable_key 始终生成相同 UUID，支持幂等 upsert。
                chunk_id = str(uuid5(NAMESPACE_URL, stable_key))
                # 创建经过 Pydantic 校验的切片，完整继承父文档治理元数据。
                chunks.append(
                    KnowledgeChunk(
                        # Qdrant Point ID 使用稳定 UUID 字符串。
                        chunk_id=chunk_id,
                        # 关联稳定父文档 ID。
                        document_id=document.document_id,
                        # 标题参与检索和引用展示。
                        title=document.title,
                        # 保存本窗口实际证据正文。
                        content=chunk_content,
                        # 保留原始可追溯来源。
                        source=document.source,
                        # 保留具体发布版本。
                        version=document.version,
                        # 保留政策生效日期。
                        effective_date=document.effective_date,
                        # 保存文档内切片序号。
                        chunk_index=chunk_index,
                        # 保存原文起点供审计回溯。
                        start_index=start,
                        # 保存原文终点供审计回溯。
                        end_index=end,
                    )
                )
                # 仅在真正生成切片后增加序号。
                chunk_index += 1

            # 到达正文末尾后立即结束，避免 overlap 把起点再次拉回最后窗口。
            if end >= len(content):
                # break 退出 while 并返回完整切片列表。
                break
            # 下一起点向前回退 overlap 个字符，保留跨切片上下文。
            next_start = end - self._chunk_overlap
            # max 确保即使边界选择异常，起点也至少前进一个字符。
            start = max(next_start, start + 1)

        # 返回当前文档经过完整元数据绑定的切片。
        return chunks

    def _choose_boundary(self, content: str, *, start: int, hard_end: int) -> int:
        """在窗口后半段寻找最靠后的自然边界，否则使用硬截断。"""

        # 已经覆盖正文末尾时无需搜索边界，直接返回真实长度。
        if hard_end >= len(content):
            # 返回正文末尾，保证最后一个字符被包含。
            return len(content)
        # 只在窗口后半段寻找边界，防止为了段落完整生成过短切片。
        search_start = start + self._chunk_size // 2
        # boundary_candidates 收集“边界结束位置”，切片会包含对应标点或换行。
        boundary_candidates: list[int] = []
        # 按从强到弱的边界遍历；最终仍通过 max 选择最靠近 hard_end 的位置。
        for boundary in ("\n\n", "\n", "。", "；", "！", "？"):
            # rfind 只搜索窗口后半段到 hard_end 之间最后一次出现的位置。
            position = content.rfind(boundary, search_start, hard_end)
            # -1 表示没有找到当前边界，不加入候选列表。
            if position != -1:
                # 加上边界自身长度，使标点或换行保留在前一个切片中。
                boundary_candidates.append(position + len(boundary))
        # 至少找到一个自然边界时选择最靠后的候选，最大化窗口利用率。
        if boundary_candidates:
            # max 返回最接近 hard_end 的自然结束位置。
            return max(boundary_candidates)
        # 没有合适自然边界时采用硬截断，保证任何长文本都能被处理。
        return hard_end
