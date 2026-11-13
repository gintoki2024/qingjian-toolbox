"""Read-only application/font diagnosis on an actual Mac; fixtures stay in --output."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path


PHRASES = ["办公说明", "步骤一：添加材料", "步骤二：检查结果"]


def compact(text):
    return re.sub(r"\s+", "", text or "")


def docx_fonts(path):
    from docx import Document
    from docx.oxml.ns import qn
    document = Document(path)
    return [{"name": node.getparent().getparent().get(qn("w:styleId")),
             "attributes": {key.rsplit("}", 1)[-1]: value for key, value in node.attrib.items()}}
            for node in document.element.xpath(".//w:rFonts") + document.styles.element.xpath(".//w:rFonts")]


def set_generated_font(source, destination, family):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    document = Document(source)

    def font(style):
        style.font.name = family
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.insert(0, fonts)
        for key in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn("w:" + key), family)
        for key in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme", "csTheme"):
            fonts.attrib.pop(qn("w:" + key), None)

    for style in document.styles:
        if hasattr(style, "font"):
            font(style)
    # The source here is a newly generated scratch DOCX; user fonts are untouched.
    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            font(run)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        font(run)
    document.save(destination)


def pdf_report(path):
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    report = {"file": str(path), "bytes": path.stat().st_size}
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    report["pypdf_text"] = text
    report["pypdf_phrases_correct"] = all(compact(phrase) in compact(text) for phrase in PHRASES)
    fonts = []
    for page in reader.pages:
        resources = page.get("/Resources", {})
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        font_resources = resources.get("/Font", {})
        font_resources = font_resources.get_object() if hasattr(font_resources, "get_object") else font_resources
        for key, value in font_resources.items():
            entry = value.get_object()
            cmap = entry.get("/ToUnicode")
            data = cmap.get_object().get_data().decode("latin-1") if cmap else ""
            fonts.append({"resource": str(key), "base_font": str(entry.get("/BaseFont")),
                          "subtype": str(entry.get("/Subtype")), "encoding": str(entry.get("/Encoding")),
                          "to_unicode": data[:30000]})
    report["pdf_fonts"] = fonts
    doc = pdfium.PdfDocument(path)
    extracted, screenshots = [], []
    try:
        for number in range(len(doc)):
            page = doc[number]
            try:
                textpage = page.get_textpage()
                try:
                    extracted.append(textpage.get_text_range())
                finally:
                    textpage.close()
                if number < 2:
                    bitmap = page.render(scale=1.5)
                    try:
                        target = path.with_name(path.stem + f"-page-{number+1}.png")
                        image = bitmap.to_pil()
                        try:
                            image.save(target)
                        finally:
                            image.close()
                        screenshots.append(str(target))
                    finally:
                        bitmap.close()
            finally:
                page.close()
    finally:
        doc.close()
    report["pdfium_text"] = "\n".join(extracted)
    report["pdfium_phrases_correct"] = all(compact(phrase) in compact(report["pdfium_text"]) for phrase in PHRASES)
    report["screenshots"] = screenshots
    return report


def available_fonts(output):
    command = ([shutil.which("fc-list"), ":", "family", "file"] if shutil.which("fc-list")
               else ["/usr/sbin/system_profiler", "SPFontsDataType", "-json"])
    result = {"command": command}
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60, check=False, text=True)
        target = output / "system-font-inventory.txt"
        target.write_text(done.stdout, encoding="utf-8")
        result.update(returncode=done.returncode, inventory=str(target), stderr=done.stderr[-2000:])
        result["candidate_mentions"] = {family: family.lower() in done.stdout.lower()
                                        for family in ("Songti SC", "PingFang SC", "Heiti SC", "Arial Unicode MS", "STSong")}
    except Exception as exc:
        result["error"] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--libreoffice")
    args = parser.parse_args()
    project, output = args.project.resolve(), args.output.resolve()
    if sys.platform != "darwin":
        parser.error("Font probe requires real macOS")
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(project))
    from qingjian.core import format_office, conversion
    engine = args.libreoffice or os.environ.get("QINGJIAN_LIBREOFFICE") or conversion.find_libreoffice()
    report = {"platform": sys.platform, "architecture": platform.machine(), "python": sys.executable,
              "project": str(project), "libreoffice": str(engine), "expected": PHRASES,
              "font_inventory": available_fonts(output), "cases": []}
    options = {"libreoffice_path": str(engine)}
    source = output / "help.md"
    source.write_text("# " + PHRASES[0] + "\n\n" + PHRASES[1] + "。\n\n" + PHRASES[2] + "。", encoding="utf-8")
    epub = output / "help.epub"
    format_office.convert(source, epub)
    for item in (source, epub):
        for suffix in (".docx", ".pdf"):
            target = output / (item.suffix[1:] + "-original" + suffix)
            row = {"case": item.suffix[1:] + "-original" + suffix}
            try:
                row["result"] = format_office.convert(item, target, options)
                if suffix == ".pdf":
                    row.update(pdf_report(target))
                else:
                    row["docx_fonts"] = docx_fonts(target)
            except Exception as exc:
                row.update(error=str(exc), traceback=traceback.format_exc())
            report["cases"].append(row)
    template = output / "md-original.docx"
    for family in ("Songti SC", "PingFang SC", "Heiti SC", "Arial Unicode MS", "STSong"):
        stem = "font-" + family.replace(" ", "-")
        docx, pdf = output / (stem + ".docx"), output / (stem + ".pdf")
        row = {"case": stem, "family": family}
        try:
            set_generated_font(template, docx, family)
            row["result"] = format_office.convert(docx, pdf, options)
            row.update(pdf_report(pdf))
        except Exception as exc:
            row.update(error=str(exc), traceback=traceback.format_exc())
        report["cases"].append(row)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": str(output / "report.json"), "architecture": platform.machine(),
                      "cases": [{"case": case["case"], "pypdf_correct": case.get("pypdf_phrases_correct"),
                                 "pdfium_correct": case.get("pdfium_phrases_correct"), "error": case.get("error")}
                                for case in report["cases"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
