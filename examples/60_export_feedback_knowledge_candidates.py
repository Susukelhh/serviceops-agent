"""导出已经人工审核的知识候选，供离线评测和版本化发布使用。"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from serviceops_agent.config.paths import PROJECT_ROOT, resolve_project_path
from serviceops_agent.config.settings import get_settings
from serviceops_agent.domain.knowledge import (
    KnowledgeAccessScope,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
)
from serviceops_agent.infrastructure.feedback_repository import (
    FeedbackRepository,
    PostgresFeedbackRepository,
    SQLiteFeedbackRepository,
)
from serviceops_agent.infrastructure.postgres_repository import PostgresConnectionPool

DEFAULT_OUTPUT = PROJECT_ROOT / "data/runtime/feedback_knowledge_candidates.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="导出反馈知识候选；不会直接修改活动知识库或Qdrant。"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def _draft_document(candidate_id: str, title: str, content: str) -> KnowledgeDocument:
    """把候选转换为不会被活动索引器读取的公共草稿文档。"""

    suffix = candidate_id.replace("-", "")[:12].upper()
    return KnowledgeDocument(
        document_id=f"KB-FEEDBACK-{suffix}",
        title=title,
        content=content,
        source=f"feedback://knowledge-candidate/{candidate_id}",
        version="candidate-1",
        effective_date=datetime.now(UTC).date(),
        status=KnowledgeDocumentStatus.DRAFT,
        access_scope=KnowledgeAccessScope.PUBLIC,
    )


def main() -> int:
    """读取当前持久化后端并写出低风险候选发布包。"""

    args = _parse_args()
    if not 1 <= args.limit <= 1000:
        raise ValueError("limit必须位于1到1000")
    settings = get_settings()
    pool: PostgresConnectionPool | None = None
    repository: FeedbackRepository
    if settings.persistence_backend == "sqlite":
        repository = SQLiteFeedbackRepository(
            database_path=resolve_project_path(settings.business_database_path)
        )
    elif settings.persistence_backend == "postgres":
        assert settings.postgres_dsn is not None
        postgres_pool: PostgresConnectionPool = ConnectionPool(
            conninfo=settings.postgres_dsn.get_secret_value(),
            kwargs={"row_factory": dict_row},
            min_size=1,
            max_size=2,
            open=True,
        )
        pool = postgres_pool
        repository = PostgresFeedbackRepository(pool=postgres_pool)
    else:
        raise RuntimeError("memory后端不会跨进程保存反馈，请使用SQLite或PostgreSQL导出")
    try:
        candidates = repository.list_knowledge_candidates(limit=args.limit)
    finally:
        if pool is not None:
            pool.close()

    draft_documents = [
        _draft_document(
            str(candidate.candidate_id),
            candidate.title,
            candidate.content,
        )
        for candidate in candidates
    ]
    payload = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate_count": len(candidates),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "draft_documents": [document.model_dump(mode="json") for document in draft_documents],
        "promotion_required": True,
        "promotion_note": (
            "草稿必须先通过检索、回答、拒答和安全回归；晋级时再写入版本化知识源并重建索引。"
        ),
    }
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"PASS: exported {len(candidates)} reviewed knowledge candidates to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
