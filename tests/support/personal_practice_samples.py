"""Known synthetic, already reviewed image content for the practice reader only."""
from backend.app.domain.enums import QuestionSourceType
from backend.app.models.subject import Subject
from tests.support.learning_content import seed_content


def seed_practice_images(engine,profile):
    def reviewed(session,question,document,assets,revision):
        question.source_type=QuestionSourceType.PAPER_IMPORTED
        revision.stem_blocks=[b for b in revision.stem_blocks if b["type"]!="original"]
        revision.admission_state="eligible"
        revision.grounding_refs={"answer_origin":"paper","fixture":"Synthetic reviewed content, no model quality claim"}
        subject=session.get(Subject,question.subject_id)
        subject.name="T049 reviewed image practice"
    return seed_content(engine,profile,configure=reviewed)
