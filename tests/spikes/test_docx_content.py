"""T002 bounded OOXML experiment. Nothing here is registered in production."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from copy import deepcopy
from html import escape
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.oxml import parse_xml
from lxml import etree
from PIL import Image, ImageDraw

from backend.app.ai.ingestion.parsers import parse_document

OUT = Path(os.environ['PL002_OUTPUT_ROOT'])
NS = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main', 'm':'http://schemas.openxmlformats.org/officeDocument/2006/math', 'a':'http://schemas.openxmlformats.org/drawingml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships', 'wp':'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'}
def tag(prefix, name): return '{'+NS[prefix]+'}'+name


def sample_docx(*, unsupported=False):
    doc=Document(); p=doc.add_paragraph('STEM BEFORE ')
    for label,color in [('INLINE','steelblue'),('FLOATING','darkorange')]:
        image=Image.new('RGB',(160,60),'white'); ImageDraw.Draw(image).text((8,15),label,fill=color)
        image_bytes=BytesIO(); image.save(image_bytes,format='PNG'); image_bytes.seek(0)
        if label=='INLINE':
            p.add_run().add_picture(image_bytes)
            p.add_run(' AFTER INLINE ')
            math=parse_xml(f'<m:oMath xmlns:m="{NS["m"]}"><m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup><m:r><m:t> + y = 7</m:t></m:r></m:oMath>')
            p._p.append(math); p.add_run(' AFTER MATH ')
        else:
            shape=p.add_run().add_picture(image_bytes)
            inline=shape._inline
            anchor=etree.Element(tag('wp','anchor'),nsmap=inline.nsmap)
            anchor.attrib.update({'distT':'0','distB':'0','distL':'0','distR':'0','simplePos':'0','relativeHeight':'0','behindDoc':'0','locked':'0','layoutInCell':'1','allowOverlap':'1'})
            simple=etree.SubElement(anchor,tag('wp','simplePos')); simple.attrib.update({'x':'0','y':'0'})
            for pos,relative in [('positionH','column'),('positionV','paragraph')]:
                node=etree.SubElement(anchor,tag('wp',pos)); node.set('relativeFrom',relative); etree.SubElement(node,tag('wp','posOffset')).text='0'
            for node in inline:
                if etree.QName(node).localname in {'extent','effectExtent'}: anchor.append(deepcopy(node))
            etree.SubElement(anchor,tag('wp','wrapNone'))
            for node in inline:
                if etree.QName(node).localname not in {'extent','effectExtent'}: anchor.append(deepcopy(node))
            inline.getparent().replace(inline,anchor); p.add_run(' AFTER FLOAT ')
    table=doc.add_table(rows=2,cols=2)
    for row,values in zip(table.rows,[['OPTION','VALUE'],['A','7']],strict=True):
        for cell,value in zip(row.cells,values,strict=True): cell.text=value
    doc.add_paragraph('EXPLANATION END')
    if unsupported:
        doc.paragraphs[-1]._p.append(parse_xml(f'<w:pict xmlns:w="{NS["w"]}"><w:unknownObject/></w:pict>'))
    output=BytesIO(); doc.save(output); return output.getvalue()


def inspect_bounded_ooxml(data):
    """Experimental finite walker, retaining original XML instead of claiming full DOCX support."""
    doc=Document(BytesIO(data)); blocks=[]; issues=[]
    tree=doc._element.getroottree()
    def emit(kind,node,**values):
        blocks.append({'kind':kind,'location':{'part':'word/document.xml','xpath':tree.getpath(node)},'original_xml':etree.tostring(node,encoding='unicode'),**values})
    def visit(node):
        name=etree.QName(node).localname
        if node.tag==tag('w','t'): emit('text',node,text=node.text or '')
        elif node.tag==tag('m','oMath'):
            # Only this finite superscript expression is rendered in the Spike.
            sup=node.find(tag('m','sSup'))
            if sup is None:
                emit('formula',node,renderable=False); issues.append('OMML_UNSUPPORTED')
            else:
                base=''.join(sup.find(tag('m','e')).itertext()); power=''.join(sup.find(tag('m','sup')).itertext())
                tail=''.join(''.join(x.itertext()) for x in node if x is not sup)
                emit('formula',node,renderable=True,mathml=f'<math xmlns="http://www.w3.org/1998/Math/MathML"><msup><mi>{escape(base)}</mi><mn>{escape(power)}</mn></msup><mtext>{escape(tail)}</mtext></math>')
        elif node.tag==tag('w','drawing'):
            blips=node.findall('.//'+tag('a','blip'))
            for blip in blips:
                rid=blip.get(tag('r','embed'))
                if not rid or rid not in doc.part.related_parts:
                    emit('unsupported',node); issues.append('DRAWING_RELATIONSHIP_UNRESOLVED'); continue
                part=doc.part.related_parts[rid]; blob=part.blob
                emit('image',node,placement='floating' if node.find('.//'+tag('wp','anchor')) is not None else 'inline',relationship_id=rid,asset_part=str(part.partname),sha256=hashlib.sha256(blob).hexdigest(),data_url='data:image/png;base64,'+base64.b64encode(blob).decode())
            if not blips: emit('unsupported',node); issues.append('DRAWING_UNSUPPORTED')
        elif node.tag==tag('w','tbl'):
            # Collect actual w:t text rather than flattening metadata/properties.
            rows=[[''.join(t.text or '' for t in c.iter(tag('w','t'))) for c in row.findall(tag('w','tc'))] for row in node.findall(tag('w','tr'))]
            emit('table',node,rows=rows)
        elif node.tag in {tag('w','pict'),tag('w','object'),tag('w','altChunk')}:
            emit('unsupported',node); issues.append('OOXML_UNSUPPORTED_'+name.upper())
        elif name not in {'pPr','rPr','sectPr'}:
            for child in node: visit(child)
    for node in doc._element.body: visit(node)
    return blocks,issues


def render_review_html(blocks,issues):
    parts=['<!doctype html><html lang="en"><meta charset="UTF-8"><title>PL002 DOCX synthetic Spike</title><style>body{font:20px system-ui;max-width:1100px;margin:36px auto}img{vertical-align:middle;max-width:180px}table{border-collapse:collapse;margin:20px 0}td{border:1px solid #888;padding:12px}math{font-size:28px}.anchor{font:11px monospace;color:#555;display:block}</style><h1>Synthetic DOCX — finite OOXML review</h1><p>Anchored order, not original floating-page layout. This is NOT the application UI.</p>']
    for block in blocks:
        kind=block['kind']
        if kind=='text': parts.append(escape(block['text']))
        elif kind=='image': parts.append('<img alt="'+block['placement']+'" src="'+block['data_url']+'">')
        elif kind=='formula': parts.append(block.get('mathml','[OMML retained; rendering unavailable]'))
        elif kind=='table': parts.append('<table>'+''.join('<tr>'+''.join('<td>'+escape(x)+'</td>' for x in row)+'</tr>' for row in block['rows'])+'</table>')
        else: parts.append('<strong>[unsupported original object retained]</strong>')
        parts.append('<small class="anchor">'+escape(block['location']['xpath'])+'</small>')
    parts.append('<p>Issues: '+escape(', '.join(issues) or 'none in finite fixture')+'</p></html>'); return ''.join(parts)


def test_python_docx_public_api_does_not_cover_all_required_objects():
    data=sample_docx(); doc=Document(BytesIO(data))
    assert [type(x).__name__ for x in doc.iter_inner_content()]==['Paragraph','Table','Paragraph']
    assert len(doc.inline_shapes)==1
    assert 'x' not in doc.paragraphs[0].text and 'STEM BEFORE' in doc.paragraphs[0].text
    print('DOCX library API: body order retained; floating picture and OMML absent from normal text/inline_shapes.')


def test_finite_ooxml_extracts_order_location_and_originals():
    data=sample_docx(); started=time.perf_counter(); blocks,issues=inspect_bounded_ooxml(data)
    kinds=[b['kind'] for b in blocks]
    assert kinds==['text','image','text','formula','text','image','text','table','text']
    assert not issues
    assert [b['placement'] for b in blocks if b['kind']=='image']==['inline','floating']
    assert next(b for b in blocks if b['kind']=='table')['rows']==[['OPTION','VALUE'],['A','7']]
    with ZipFile(BytesIO(data)) as z:
        tree=etree.ElementTree(etree.fromstring(z.read('word/document.xml')))
        for block in blocks:
            found=tree.xpath(block['location']['xpath'],namespaces=NS)
            assert len(found)==1
            assert etree.tostring(found[0],encoding='unicode')==block['original_xml']
            if block['kind']=='image': assert hashlib.sha256(z.read(block['asset_part'].lstrip('/'))).hexdigest()==block['sha256']
    output=OUT/'docx'; output.mkdir(exist_ok=True)
    (output/'synthetic.docx').write_bytes(data)
    (output/'review.html').write_text(render_review_html(blocks,issues),encoding='utf-8')
    result={'sample_bytes':len(data),'block_kinds':kinds,'anchors_resolved':len(blocks),'images':2,'limited_omml_rendered':True,'elapsed_seconds':round(time.perf_counter()-started,4),'unsupported_boundary':'arbitrary OMML, VML/OLE/SmartArt, nested/merged table layout and original page geometry not supported or tested'}
    (output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8'); print(json.dumps(result))


def test_unsupported_object_is_not_silently_dropped():
    blocks,issues=inspect_bounded_ooxml(sample_docx(unsupported=True))
    assert issues==['OOXML_UNSUPPORTED_PICT']
    assert blocks[-1]['kind']=='unsupported' and 'unknownObject' in blocks[-1]['original_xml']


def _minimal_docx(body: str) -> bytes:
    """Stdlib-only synthetic DOCX; python-docx is not a declared dependency."""
    from zipfile import ZIP_DEFLATED, ZipFile
    w = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml',
            f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    return output.getvalue()


def test_production_docx_is_wired_after_t027_without_full_support_claim():
    """TCR-T027: the spike's “production DOCX unimplemented” fact was replaced.

    T027 explicitly authorises wiring DOCX into ``parsers.py``. The boundary
    assertions are kept: unsupported objects are retained with a reason and the
    finite walker is not claimed to support arbitrary OOXML.
    """
    parsed = parse_document('synthetic.docx', _minimal_docx(
        '<w:p><w:r><w:t>STEM BEFORE</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'))
    assert parsed.file_format == 'docx'
    assert [b.kind for b in parsed.blocks] == ['text', 'table']
    assert parsed.blocks[1].rows == (('A',),)
    blocked = parse_document('unsupported.docx', _minimal_docx(
        '<w:p><w:r><w:t>END</w:t></w:r><w:pict><w:unknownObject/></w:pict></w:p>'))
    assert blocked.issues == ('OOXML_UNSUPPORTED_PICT',)
    assert [b.kind for b in blocked.blocks] == ['text', 'original']
