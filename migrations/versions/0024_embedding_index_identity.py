"""T214：为 KnowledgeBase 增加可空 Embedding 索引语义身份。

只新增 ``knowledge_bases.embedding_identity`` JSONB，不改变 ``document_chunks``
的 ``vector(1024)``、HNSW/GIN 索引或任何历史向量。历史 NULL 由领域服务只读映射为
``legacy_unknown``，迁移不回填。

降级刻意不删除该列：身份是历史接纳审计的一部分，回退应用版本不能销毁已记录的
历史事实；重新升级通过列存在检查保持幂等。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_embedding_index_identity"
down_revision: str | None = "0023_grading_exam_question"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """仅当列缺失时增加可空 JSONB；绝不修改既有向量或索引。"""

    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_bases")}
    if "embedding_identity" not in columns:
        op.add_column(
            "knowledge_bases",
            sa.Column(
                "embedding_identity",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    """保留身份列，避免回退删除历史接纳事实或影响既有向量。"""

    # Intentionally no-op. Re-upgrade is idempotent; destructive cleanup requires
    # a separately reviewed migration and explicit data-owner approval.
    return
