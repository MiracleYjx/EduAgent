"""Actual configuration/snapshots with real local OCR and explicit Fake LLM transport."""
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from backend.app.services.learning_operations import LearningOperations
from tests.support.runtime_settings_fakes import settings_harness


def extraction_oracle(payload: dict) -> dict:
    document = payload["document_id"]
    blocks = payload["blocks"]
    sources = [{"document_id":document,"page":b.get("page"),"anchor":b["anchor"]} for b in blocks]
    text = [b for b in blocks if b["type"]=="text" and str(b.get("text") or "").strip()]
    def locations(needle):
        selected=[b for b in text if needle.casefold() in "".join(str(b["text"]).split()).casefold()]
        return [{"document_id":document,"page":b.get("page"),"anchor":b["anchor"]} for b in selected] or sources
    def question(kind,stem,options,loc):
        return {"question_type":kind,"stem_blocks":[{"type":"text","text":stem}],
                "options":[{"id":k,"blocks":[{"type":"text","text":v}]} for k,v in options],
                "source_locations":loc,"uncertainties":[]}
    if any("Choose the matching figure" in str(b.get("text")) for b in text):
        assets=payload["assets"]
        options=[{"id":chr(65+i),"blocks":[{"type":"image","asset_id":a["id"],"alt":"Original option figure"}]}
                 for i,a in enumerate(assets)]
        first=question("single_choice","Choose the matching figure.",[],sources)
        first["options"]=options
        second=question("short_answer","Evaluate at x = 2:",[],sources)
        second["stem_blocks"].append({"type":"formula","expression":{"kind":"row","children":[
            {"kind":"identifier","text":"x"},{"kind":"operator","text":"+"},{"kind":"number","text":"1"}]}})
        third=question("multiple_choice","Choose the values shown in the table.",[("A","7"),("B","8")],sources)
        third["stem_blocks"].append({"type":"table","headers":["OPTION","VALUE"],"rows":[["A","7"]]})
        return {"questions":[first,second,third]}
    first=question("single_choice","What is 2 + 2?",[("A","4"),("B","5")],locations("1."))
    answer_blocks=[b for b in text if "Answer:A".casefold() in "".join(str(b["text"]).split()).casefold()]
    if answer_blocks:
        b=answer_blocks[0];loc={"document_id":document,"page":b.get("page"),"anchor":b["anchor"]}
        if loc not in first["source_locations"]:first["source_locations"].append(loc)
        first.update(reference_answer="A",answer_origin="paper",
                     evidence={"reference_answer":{"source":loc,"text":b["text"]}})
    return {"questions":[first,
        question("multiple_choice","Choose the even numbers.",[("A","2"),("B","3"),("C","4")],locations("2.")),
        question("short_answer","Explain why 2 + 2 = 4.",[],locations("3."))]}


def answer_oracle(payload:dict)->dict:
    kind=payload["content"]["question_type"]
    stem=" ".join(b.get("text","") for b in payload["content"]["stem_blocks"])
    keys={option["id"] for option in payload["content"]["options"]}
    missing_answer=("A,C" if "C" in keys else "A") if kind=="multiple_choice" else (
        "3" if "Evaluate at x = 2" in stem else "4") if kind=="short_answer" else "A"
    return {
        "reference_answer":payload.get("reference_answer") or (
            missing_answer),
        "scoring_rubric":payload.get("scoring_rubric") if payload["mode"]=="review" else
            "Explain the stated addition correctly for full credit." if kind=="short_answer" else None,
        "explanation_blocks":[{"type":"text","text":"Synthetic Fake result for flow verification."}],
        "quality":{"answer_basis":"Synthetic derivation from the explicitly supplied conditions.",
            "assessment":{"checks":[{"kind":k,"verdict":"pass","reason":"Synthetic test oracle"}
                for k in ("answer_correctness","condition_sufficiency","option_ambiguity","rubric_clarity")],
                "issues":[]}},
    }


async def configured_paper_runtime(root:Path,environment_id:UUID,model_dir:Path,sessions):
    from backend.app.ai.ingestion.ocr.rapidocr import MODEL_ID, _model_paths
    _model_paths(model_dir)
    harness=settings_harness(root,environment_id=environment_id)
    values=harness.values("synthetic-personal-paper")
    pid,revision=uuid4(),uuid4()
    values["providers"][str(pid)]={
        "profile_id":str(pid),"profile_revision":str(revision),"display_name":"Real local OCR for Test",
        "adapter_id":"rapidocr","model_id":MODEL_ID,"resource_directory":str(model_dir),
        "auth_mode":"none","declared_capabilities":["ocr"],"enabled":True,
    }
    values["bindings"]["ocr"]={"task_kind":"ocr","profile_id":str(pid),"profile_revision":str(revision)}
    draft=harness.draft(values)
    probes=await harness.admission.evidence(draft)
    ocr=next(p for p in draft.values.providers.values() if p.adapter_id=="rapidocr")
    probe=await harness.admission.probes.run(candidate_revision=draft.revision_id,profile=ocr,
                                            capability="ocr",allow_billable=False)
    assert probe.safe_error_code=="SUCCESS",probe.safe_error_code
    harness.service.activate(draft.revision_id,expected_revision=None,
                             probe_ids=[p.probe_id for p in [*probes,probe]],process_env={})
    calls=[]
    async def handler(request):
        body=json.loads(request.content)
        payload=json.loads(body["messages"][-1]["content"])
        if "practice_id" in payload:
            from tests.integration.test_personal_practice_flow import fake_generation
            purpose="personal_generation";result=fake_generation(payload)
        elif "document_id" in payload:
            purpose="paper_extraction";result=extraction_oracle(payload)
        elif "question_id" in payload:
            purpose="answer_completion";result=answer_oracle(payload)
        else:
            raise AssertionError("No Fake oracle for this operation")
        calls.append({"purpose":purpose,"model":body["model"]})
        return httpx.Response(200,json={
            "id":"fake-personal-"+str(len(calls)),"object":"chat.completion","created":0,
            "model":"synthetic-personal-paper",
            "choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(result)},
                        "finish_reason":"stop"}],
            "usage":{"prompt_tokens":100,"completion_tokens":100,"total_tokens":200},
        })
    operations=LearningOperations(sessions=sessions,config=harness.service,
                                 transport=httpx.MockTransport(handler))
    return harness,operations,calls
