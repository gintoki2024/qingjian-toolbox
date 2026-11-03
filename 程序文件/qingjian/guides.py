"""Task-oriented help and harmless practice data generated locally on demand."""
from __future__ import annotations

import html
from pathlib import Path


GUIDES = {
    "excel_compare": {
        "intro": "把同一批业务的两份数据按编号对应起来，找出新增、删除和金额等字段变化。",
        "quick": "① 添加旧表和新表　② 填写能唯一识别记录的“主键列”　③ 开始处理后查看差异清单。",
        "steps": ["添加恰好两份表格；展开“文件清单”，把旧表放在第一位、新表放在第二位。",
                  "点击“查看列名”，把两份表都存在的编号列填入“主键列”。多列联合识别时用逗号分隔。",
                  "标题在第1行就保留默认表头行；表中有说明行时改成实际表头行号。",
                  "点击开始处理，结果表会列出对账摘要、新增记录、删除记录、字段修改和主键异常；成功匹配数见摘要。"],
        "terms": [("主键列", "用于识别同一条业务的列，例如合同编号、员工编号。姓名可能重名，可用“部门，姓名”组合。"),
                  ("数值允许误差", "0表示精确比较；填0.01表示数值差在0.01以内可视为相同。编号等文本不按数值误差比较。"),
                  ("重复或空主键", "会单独列为异常，先核实数据，再重新对账。")],
        "example": "示例用“编号”对应：0001的金额由100变成120，0003被删除，0004为新增，0002保持一致。",
    },
    "excel_clean": {
        "intro": "把表格整理成可汇总的数据，同时找出空值、重复记录和无法转换的内容。",
        "quick": "① 添加数据表　② 按实际列名选择去重、必填、日期等规则　③ 查看“清洗结果”和“异常记录”。",
        "steps": ["添加需要整理的表格，点击“查看列名”确认表头用词。",
                  "先使用清除首尾空白、删除整行空白；需要去重时填写依据列，例如“编号”。留空则整行完全相同才算重复。",
                  "按需要填写必填列、日期列、数值列和文本列；同一列不要同时作为数值列与文本列。",
                  "开始处理后，先检查“异常记录”，再使用清洗结果。无法转换的值会保留，供人工修正。"],
        "terms": [("必填列", "检查姓名、部门等列是否有漏填；不会替你猜测或补数据。"),
                  ("统一数值的列", "把金额一类的数字文本转为数值，无法识别的内容列入异常。"),
                  ("作为文本的列", "编号、身份证号等不参与计算的列。001这样的前导零可保留；已在Excel中被舍入的长数字无法恢复。")],
        "example": "示例包含姓名两侧空格、一条重复编号、空姓名和非法金额。默认示例设置会清理空格、去重，并列出需要人工确认的内容。",
    },
    "check_materials": {
        "intro": "按你填写的清单检查每个文件夹是否缺件，并检查其中表格的必填信息。",
        "quick": "① 添加每个项目的材料文件夹　② 列出应有文件或表格必填列　③ 查看缺件与空值清单。",
        "steps": ["使用“文件夹”添加待检查的项目目录；每个加入的目录都会独立检查。",
                  "“应有材料”每行写一个文件名或匹配规则，例如“通知*.pdf”“名单.xlsx”“附件/*.docx”。",
                  "星号表示任意文字。“附件/*.docx”表示附件子文件夹下的Word材料；路径相对于本次加入的项目目录。",
                  "需要检查表格时填写必填列，例如“姓名，部门”。开始后查看缺件、缺列和空值位置。"],
        "terms": [("包含子文件夹", "控制是否递归检查目录；附件放在子文件夹时建议开启。"),
                  ("材料完整", "表示文件存在及所选字段检查通过，不代表材料内容、签字盖章或审批有效性已核实。"),
                  ("表头所在行", "只用于检查表格必填信息，应与表格实际标题行一致。")],
        "example": "示例有“项目甲”和“项目乙”两个目录。甲资料齐全；乙缺少通知和附件，名单中还有未填部门。",
    },
    "template_fill": {
        "intro": "选择Word或PPT模板，把数据表每一行填成一个单独文件。",
        "quick": "① 上方添加数据表　② 下方选择DOCX/PPTX模板　③ 模板写入 {{列名}}，一行生成一份文件。",
        "steps": ["先准备XLSX或CSV数据表，第一行是字段名，例如“姓名”“部门”“日期”。上方添加的是这份数据表。",
                  "在Word正文、表格或PPT文本框、表格中写入 {{姓名}}、{{部门}}，再在“文档模板”处选择它。",
                  "文件名规则可填“通知_{姓名}_{序号:03d}”；只填列名和序号，不写程序代码。",
                  "点击开始处理，每行数据生成一份同类型文档，并生成逐行处理报告。"],
        "terms": [("PPT模板", "支持.pptx，旧.ppt请先另存为.pptx。保留模板布局、图片和文本样式；图片内文字无法填充。"),
                  ("缺字段时", "默认跳过并记录错误。选择留空时，缺失内容会填为空白，应人工复核。"),
                  ("同时生成PDF", "适用于Word模板；PPT模板会生成可编辑PPTX。长文字可能超出文本框，需查看成稿。")],
        "example": "例如数据行“张三 / 综合部”，模板中“欢迎 {{姓名}} 加入 {{部门}}”会填成“欢迎 张三 加入 综合部”。",
    },
}


from .feature_registry import GUIDES as OFFICE_GUIDES
GUIDES.update(OFFICE_GUIDES)


def guide_html(mode):
    spec = GUIDES[mode]
    parts = ["<style>body{font:14px 'Microsoft YaHei';color:#28314a}h2{color:#5352cc}li{margin:10px 0}p{line-height:1.6}</style>",
             "<p>" + html.escape(spec["intro"]) + "</p><h2>怎么操作</h2><ol>"]
    parts.extend("<li>" + html.escape(text) + "</li>" for text in spec["steps"])
    parts.append("</ol><h2>字段怎么填</h2>")
    parts.extend("<p><b>" + html.escape(title) + "：</b>" + html.escape(text) + "</p>" for title, text in spec["terms"])
    parts.append("<h2>一个例子</h2><p>" + html.escape(spec["example"]) + "</p>")
    return "".join(parts)


def build_example(mode, folder):
    """Return practice sources/options; never read or change real user input."""
    from .feature_registry import SPECS as OFFICE_SPECS, build_example as office_example
    if mode in OFFICE_SPECS:
        return office_example(mode, folder)
    from openpyxl import Workbook

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    def book(path, rows):
        workbook = Workbook()
        page = workbook.active
        page.title = "数据"
        for row in rows:
            page.append(row)
        page.freeze_panes = "A2"
        for column in page.columns:
            page.column_dimensions[column[0].column_letter].width = 20
        workbook.save(path)
        workbook.close()
        return str(path)

    if mode == "excel_compare":
        head = ["编号", "姓名", "金额"]
        old = book(folder / "01_旧表.xlsx", [head, ["0001", "张三", 100], ["0002", "李四", 200], ["0003", "王五", 300]])
        new = book(folder / "02_新表.xlsx", [head, ["0001", "张三", 120], ["0002", "李四", 200], ["0004", "赵六", 400]])
        return [old, new], {"key_columns": ["编号"], "header_row": 1, "numeric_tolerance": 0, "sheet_left": "", "sheet_right": "", "ignore_whitespace": True}
    if mode == "excel_clean":
        source = book(folder / "待清洗数据.xlsx", [["编号", "姓名", "部门", "金额", "入职日期"],
            ["0001", " 张三 ", "综合部", "1,200.50", "2026/10/01"],
            ["0001", " 张三 ", "综合部", "1,200.50", "2026/10/01"],
            ["0002", "", "业务部", "待补充", "待定"], [None, None, None, None, None]])
        return [source], {"sheet": "", "header_row": 1, "trim_text": True, "remove_blank_rows": True,
                          "deduplicate": True, "key_columns": ["编号"], "required_columns": ["姓名", "部门"],
                          "date_columns": ["入职日期"], "number_columns": ["金额"], "text_columns": ["编号"], "recursive": True}
    if mode == "check_materials":
        from docx import Document
        sources = []
        for name, complete in (("项目甲_齐全示例", True), ("项目乙_缺件示例", False)):
            directory = folder / name
            directory.mkdir()
            sources.append(str(directory))
            book(directory / "名单.xlsx", [["姓名", "部门"], ["张三", "综合部" if complete else ""]])
            if complete:
                (directory / "通知.txt").write_text("此文件为上手练习使用的通知示例。", encoding="utf-8")
                (directory / "附件").mkdir()
                document = Document()
                document.add_paragraph("上手练习附件")
                document.save(directory / "附件" / "说明.docx")
        return sources, {"required_files": ["通知.txt", "名单.xlsx", "附件/*.docx"],
                         "required_columns": ["姓名", "部门"], "header_row": 1, "recursive": True}
    raise ValueError("当前工具没有一键练习数据。")
