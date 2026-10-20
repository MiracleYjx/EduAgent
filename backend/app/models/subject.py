"""Personal space knowledge identities and immutable material version references."""

from uuid import UUID

from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Subject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subjects"
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None]


class KnowledgePoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_points"
    __table_args__ = (UniqueConstraint("id", "subject_id"),)
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(160))
    confirmation_state: Mapped[str] = mapped_column(
        String(32), server_default="pending"
    )


class MaterialSeries(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "material_series"
    __table_args__ = (
        UniqueConstraint("id", "subject_id"),
        ForeignKeyConstraint(
            ["default_version_id", "id"],
            ["material_versions.id", "material_versions.series_id"],
            name="fk_material_default_version",
            use_alter=True,
        ),
    )
    subject_id: Mapped[UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(160))
    default_version_id: Mapped[UUID | None] = mapped_column(Uuid)


class MaterialVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "material_versions"
    __table_args__ = (
        UniqueConstraint("id", "series_id"),
        UniqueConstraint("id", "subject_id"),
        ForeignKeyConstraint(
            ["series_id", "subject_id"],
            ["material_series.id", "material_series.subject_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "subject_id"],
            ["documents.id", "documents.subject_id"],
            ondelete="RESTRICT",
        ),
    )
    series_id: Mapped[UUID] = mapped_column(Uuid)
    subject_id: Mapped[UUID] = mapped_column(Uuid)
    document_id: Mapped[UUID] = mapped_column(Uuid)
    label: Mapped[str] = mapped_column(String(160))


chapter_knowledge_points = Table(
    "chapter_knowledge_points",
    Base.metadata,
    Column("chapter_id", Uuid, primary_key=True),
    Column("knowledge_point_id", Uuid, primary_key=True),
    Column("subject_id", Uuid, nullable=False),
    ForeignKeyConstraint(
        ["chapter_id", "subject_id"],
        ["chapters.id", "chapters.subject_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["knowledge_point_id", "subject_id"],
        ["knowledge_points.id", "knowledge_points.subject_id"],
        ondelete="RESTRICT",
    ),
)
