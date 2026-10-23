"""Zero HTTP is an explicit local embedding fact, never a remote budget bypass."""
import pytest
from pydantic import ValidationError

from backend.app.services.learning_operations import StepPlan


@pytest.mark.parametrize("kind,local,budget",[("embedding",False,0),("default_llm",True,0),("embedding",True,1)])
def test_zero_budget_only_for_explicit_local_embedding(kind,local,budget):
    with pytest.raises(ValidationError):
        StepPlan(step_id="test",task_kind=kind,messages=[],request_budget=budget,local_only=local)

def test_local_embedding_plan_records_zero_remote_budget():
    step=StepPlan(step_id="local",task_kind="embedding",messages=[],request_budget=0,local_only=True)
    assert step.request_budget==0 and step.local_only
