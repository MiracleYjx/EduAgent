"""Synthetic personal-paper format oracles; no user files or generated reports."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw, ImageFont

KINDS = ("txt", "md", "pdf", "scanned_pdf", "docx", "png", "jpeg")
LINES = (
    "1. What is 2 + 2?", "A. 4", "B. 5", "Answer: A",
    "2. Choose the even numbers.", "A. 2", "B. 3", "C. 4",
    "3. Explain why 2 + 2 = 4.",
)


def personal_sample(kind: str, *, stress: bool = False) -> tuple[str, bytes]:
    suffix = "stress" if stress else "clear"
    text = "\n".join(LINES[:4])+"\n\n"+"\n".join(LINES[4:8])+"\n\n"+LINES[8]
    if kind in {"txt","md"}:
        if stress:
            text += "\n\nOriginal formula: x^2 + 1 = 5.\nOriginal table:\nValue | Count\n2 | 4"
        return suffix+"."+kind, text.encode("utf-8")
    if kind == "docx":
        from tests.contract.test_learning_materials import sample_docx
        payload = sample_docx(images=8 if stress else 2)
        output = BytesIO()
        with ZipFile(BytesIO(payload)) as old, ZipFile(output,"w",ZIP_DEFLATED) as new:
            for item in old.infolist():
                data=old.read(item.filename)
                if item.filename=="word/document.xml":
                    value=data.decode("utf-8").replace("BEFORE IMAGE","1. Choose the matching figure.")
                    for index in range(1,9):
                        value=value.replace("AFTER IMAGE "+str(index),chr(64+index)+". original option")
                    value=value.replace("<m:oMath>","</w:p><w:p><w:r><w:t>2. Evaluate at x = 2: </w:t></w:r><m:oMath>")
                    value=value.replace("AFTER MATH","Explain your reasoning.")
                    value=value.replace("<w:tbl>","<w:p><w:r><w:t>3. Choose the values shown in the table.</w:t></w:r></w:p><w:tbl>")
                    value=value.replace("EXPLANATION END","A. 7 B. 8")
                    data=value.encode("utf-8")
                new.writestr(item.filename,data)
        return suffix+".docx",output.getvalue()
    if kind == "pdf":
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer=PdfWriter()
        groups=[LINES[:2],LINES[2:]] if stress else [LINES]
        for lines in groups:
            page=writer.add_blank_page(width=595,height=842)
            font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),
                NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
            page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({
                NameObject("/F1"):writer._add_object(font)})})
            stream=DecodedStreamObject()
            content="BT /F1 16 Tf 45 770 Td 28 TL "
            for index,line in enumerate(lines):
                escaped=line.replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
                content+=("T* " if index else "")+"("+escaped+") Tj "
            stream.set_data((content+"ET").encode("ascii"))
            page[NameObject("/Contents")]=writer._add_object(stream)
        output=BytesIO();writer.write(output)
        return suffix+".pdf",output.getvalue()
    image=Image.new("RGB",(900,1100),"white")
    draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=28)
    for index,line in enumerate(LINES):
        draw.text((40,35+index*80),line,font=font,fill="black")
    if stress:
        smaller=image.resize((450,550)).rotate(2,expand=False,fillcolor="white")
        image.close();image=smaller
    output=BytesIO()
    if kind=="scanned_pdf":
        image.save(output,format="PDF",resolution=150)
        extension="pdf"
    elif kind=="png":
        image.save(output,format="PNG");extension="png"
    elif kind=="jpeg":
        image.save(output,format="JPEG",quality=25 if stress else 90);extension="jpg"
    else:
        image.close();raise ValueError("Unknown format")
    image.close()
    return suffix+"."+extension,output.getvalue()
