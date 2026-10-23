"""Subject-owned chat lifecycle; no grade/history mutation dependencies."""

from uuid import UUID, uuid4

from sqlalchemy import delete, select

from backend.app.models.conversation import Conversation, Message
from backend.app.models.operation_run import OperationRun
from backend.app.schemas.personal_teacher import (
    ConversationCreate,
    ConversationSummary,
    ConversationView,
    TeacherMessageView,
)
from backend.app.services.learning_material_service import MaterialError


class ConversationService:
    def __init__(self, space):
        self.space = space
        self.sessions = space.sessions

    def _conversation(self, session, identity: UUID, subject_id: UUID, *, lock=False):
        self.space._check_space()
        row = session.get(
            Conversation, identity, with_for_update=lock, populate_existing=lock
        )
        if row is None or row.subject_id != subject_id:
            raise MaterialError("CONVERSATION_NOT_FOUND", 404)
        return row

    @staticmethod
    def summary(row):
        return ConversationSummary(
            id=row.id,
            subject_id=row.subject_id,
            title=row.title,
            created_at=row.created_at,
            pending_operation_id=row.pending_operation_id,
        )

    def list(self, subject_id: UUID):
        self.space.get(subject_id)
        with self.sessions() as session:
            return [
                self.summary(row)
                for row in session.scalars(
                    select(Conversation)
                    .where(Conversation.subject_id == subject_id)
                    .order_by(Conversation.created_at.desc(), Conversation.id)
                )
            ]

    def create(self, subject_id: UUID, body: ConversationCreate):
        self.space.get(subject_id)
        with self.sessions.begin() as session:
            row = Conversation(id=uuid4(), subject_id=subject_id, title=body.title)
            session.add(row)
            session.flush()
            return self.summary(row)

    def get(self, identity: UUID, subject_id: UUID):
        with self.sessions() as session:
            row = self._conversation(session, identity, subject_id)
            messages = list(
                session.scalars(
                    select(Message)
                    .where(Message.conversation_id == identity)
                    .order_by(Message.created_at, Message.id)
                )
            )
            return ConversationView(
                **self.summary(row).model_dump(),
                messages=[
                    TeacherMessageView(
                        id=m.id,
                        role=m.role,
                        text=m.text,
                        state=m.state,
                        operation_id=m.operation_id,
                        reply=m.result,
                        citations=m.citations or [],
                        error_code=m.error_code,
                        runtime_snapshot_id=m.runtime_snapshot_id,
                        created_at=m.created_at,
                    )
                    for m in messages
                ],
            )

    def delete(self, identity: UUID, subject_id: UUID):
        with self.sessions.begin() as session:
            row = self._conversation(session, identity, subject_id, lock=True)
            if row.pending_operation_id:
                operation = session.get(OperationRun, row.pending_operation_id)
                message = session.scalar(
                    select(Message).where(
                        Message.operation_id == row.pending_operation_id,
                        Message.role == "user",
                    )
                )
                if (
                    operation
                    and operation.state == "running"
                    or message
                    and message.state == "running"
                ):
                    raise MaterialError("TEACHER_RUNNING", 409)
            # DB CASCADE is only conversations -> own messages. Operation accounting
            # remains durable; its private chat input is never exposed by public GET.
            session.execute(delete(Conversation).where(Conversation.id == identity))
