"""T003: real local OCR plus explicitly separate Fake extraction provenance tests."""
from __future__ import annotations
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import time
import unicodedata
from uuid import uuid4
from PIL import Image, ImageDraw, ImageFont
import pytest
from backend.app.ai.ingestion.paper_pipeline import open_paper
from backend.app.ai.ingestion.parsers import DocumentParseError, parse_document
from backend.app.ai.ingestion.ocr.rapidocr import RapidOCRProvider, _model_paths
from backend.app.ai.ingestion.ocr.base import OCRProviderError
from backend.app.ai.paper_extraction.service import PaperExtractor, PaperExtractionError, TextPage

OUT=Path(os.environ['PL002_OUTPUT_ROOT'])
MODELS=Path(os.environ['PL002_OCR_MODELS'])

def synthetic_pages():
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',32)
    small=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',28)
    lines=[['1. 已知矩形长为 8 厘米，宽为 3 厘米。','请结合下一页选项，选择矩形的面积。'],['1. 续题：选择正确的面积。','A. 24 平方厘米    B. 11 平方厘米','2. 若 f = 2x + 3，x = 4，求 f。','表格：数量与价格','数量  2  4','价格  6  12']]
    pages=[]
    for index,texts in enumerate(lines):
        img=Image.new('RGB',(1200,900),'white'); draw=ImageDraw.Draw(img)
        for row,text in enumerate(texts): draw.text((60,55+row*65),text,font=font,fill='black')
        if index==0:
            draw.rectangle((100,390,550,560),outline='black',width=4)
            draw.text((260,570),'8 cm',font=font,fill='black'); draw.text((570,450),'3 cm',font=font,fill='black')
        else:
            # A true superscript and a rule table are intentionally not plain text.
            draw.text((60,510),'x',font=font,fill='black'); draw.text((80,493),'2',font=small,fill='black')
            draw.text((112,510),'+ 1 = 5',font=font,fill='black')
            for x in (60,300,540): draw.line((x,260,x,455),fill='black',width=2)
            for y in (260,325,390,455): draw.line((60,y,540,y),fill='black',width=2)
            draw.text((650,180),'A',font=font,fill='black'); draw.rectangle((700,180,940,300),outline='black',width=4)
            draw.text((650,350),'B',font=font,fill='black'); draw.polygon([(700,450),(820,330),(940,450)],outline='black',width=4)
        pages.append(img)
    return pages,lines


def edit_distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nxt=[i]
        for j,y in enumerate(b,1): nxt.append(min(nxt[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=nxt
    return row[-1]


def normalized(s): return ''.join(unicodedata.normalize('NFKC',s).split())


def test_real_local_ocr_scanned_pdf_png_jpeg_and_stress():
    output=OUT/'ocr'; output.mkdir(exist_ok=True)
    checked=_model_paths(MODELS); assert len(checked)==3
    pages,lines=synthetic_pages(); payload=BytesIO(); pages[0].save(payload,format='PDF',save_all=True,append_images=pages[1:],resolution=150.0)
    pdf=payload.getvalue(); (output/'synthetic-scanned.pdf').write_bytes(pdf)
    with pytest.raises(DocumentParseError) as exc: parse_document('scan.pdf',pdf)
    assert exc.value.error_code=='DOCUMENT_NO_TEXT'
    with open_paper(pdf) as paper:
        rendered=list(paper.pages()); assert len(rendered)==2 and all(p.needs_ocr and not p.text for p in rendered)
        assert [p.page_number for p in rendered]==[1,2]
    provider=RapidOCRProvider(MODELS); variants=[]
    for p in rendered:
        path=output/f'scan-page-{p.page_number}.png'; path.write_bytes(p.png); variants.append((path,lines[p.page_number-1],f'scan-pdf-page-{p.page_number}'))
    for fmt,quality in [('PNG',None),('JPEG',85)]:
        buf=BytesIO(); pages[0].save(buf,format=fmt,**({'quality':quality} if quality else {}))
        with open_paper(buf.getvalue()) as paper:
            decoded=list(paper.pages()); assert len(decoded)==1 and decoded[0].needs_ocr
        path=output/('single.'+('png' if fmt=='PNG' else 'jpg')); path.write_bytes(buf.getvalue()); variants.append((path,lines[0],fmt))
    stress=pages[1].resize((400,300)).rotate(2,expand=False,fillcolor='white'); stress_path=output/'low-res-rotated.jpg'; stress.save(stress_path,quality=25); variants.append((stress_path,lines[1],'stress-400x300-jpeg25-rotate2'))
    results=[]
    for path,reference,label in variants:
        start=time.perf_counter(); ocr=asyncio.run(provider.extract_text(path)); elapsed=time.perf_counter()-start
        assert ocr.regions and ocr.text
        for region in ocr.regions: assert region.bbox[0]>=0 and region.bbox[1]>=0
        expected=normalized(''.join(reference)); actual=normalized(ocr.text)
        exact=[normalized(line) in actual for line in reference]
        # CER over the whole page includes deliberately additional diagram/formula labels.
        row={'variant':label,'input_bytes':path.stat().st_size,'image_size':list(Image.open(path).size),'seconds':round(elapsed,4),'regions':len(ocr.regions),'exact_body_lines':sum(exact),'body_lines':len(reference),'body_line_matches':exact,'whole_page_cer_with_extra_labels':round(edit_distance(expected,actual)/len(expected),4),'recognized_text':ocr.text,'confidence_is_accuracy':False}
        results.append(row)
    for img in pages: img.close()
    summary={'provider':'RapidOCR 3.9.2 / ONNX CPU / preinstalled PP-OCRv5-mobile (3 SHA verified)','results':results,'limitations':['CER includes diagram/formula labels and is not a representative corpus accuracy','OCR regions do not establish geometry meaning or original table cell structure','superscript/line-grid reading order and option-image ownership require original-region review','no real Vision or LLM called; cross-page extraction below is Fake provenance only']}
    (output/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(summary,ensure_ascii=False))


class ScriptedExtractor:
    def __init__(self,outputs): self.outputs=outputs; self.calls=[]
    async def generate_structured(self,messages,schema,**kwargs):
        self.calls.append(json.loads(messages[-1]['content'])); return schema.model_validate(self.outputs[len(self.calls)-1])


def test_fake_cross_page_pending_and_option_order_only_proves_source_checks():
    first='1. rectangle length 8, width 3; continued on next page.'
    second='1. continued: select area. B. 11 A. 24'
    p1,p2=TextPage(uuid4(),1,first,'OCR'),TextPage(uuid4(),2,second,'OCR')
    fake=ScriptedExtractor([{'pending':[{'page_number':1,'text':first}]},{'questions':[{'source_pages':[1,2],'source_anchor':{'page_number':1,'text':first},'question_number':'1','question_type':'SINGLE_CHOICE','content':'rectangle length 8, width 3; select area.','options':{'A':'24','B':'11'},'option_sources':{'A':{'page_number':2,'text':'A. 24'},'B':{'page_number':2,'text':'B. 11'}}}]}])
    async def run(): return [x async for x in PaperExtractor(fake,batch_size=1).extract([p1,p2])]
    batches=asyncio.run(run()); question=batches[1].questions[0]
    assert fake.calls[1]['pending']==[{'page_number':1,'text':first}]
    assert question.source_page_ids==[p1.id,p2.id] and list(question.options)==['B','A']
    assert question.reference_answer is None and question.assets is None and question.source_regions is None
    assert len(fake.calls)==2
    print('Cross-page Fake: original pages/pending/option reading order verified; assets/regions remain None, no image ownership claim.')


@pytest.mark.parametrize('case',['invented_excerpt','final_pending','wrong_page'])
def test_fake_bad_provenance_is_rejected(case):
    page=TextPage(uuid4(),1,'1. actual source only','OCR')
    if case=='final_pending': batch={'pending':[{'page_number':1,'text':page.text}]}
    else: batch={'questions':[{'source_pages':[1 if case=='invented_excerpt' else 2],'source_anchor':{'page_number':1,'text':'made up' if case=='invented_excerpt' else page.text},'content':'question'}]}
    fake=ScriptedExtractor([batch])
    async def run(): return [x async for x in PaperExtractor(fake).extract([page])]
    with pytest.raises(PaperExtractionError): asyncio.run(run())
    assert len(fake.calls)==1


def test_missing_ocr_models_are_explicit_and_not_downloaded(tmp_path):
    path=tmp_path/'synthetic.png'; Image.new('RGB',(100,80),'white').save(path)
    provider=RapidOCRProvider(tmp_path/'missing-models')
    with pytest.raises(OCRProviderError) as exc: asyncio.run(provider.extract_text(path))
    assert exc.value.code=='OCR_PROVIDER_NOT_READY'
