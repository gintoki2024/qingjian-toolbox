"""Chinese tool catalog and declarative settings forms."""
from __future__ import annotations

EXCEL_TYPES = {".xlsx", ".xlsm", ".csv", ".tsv"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
TEXT_TYPES = EXCEL_TYPES | {".docx", ".pdf", ".txt", ".md", ".html", ".htm", ".xhtml"}


def field(key, title, kind="text", default="", **kwargs):
    return dict(key=key, title=title, kind=kind, default=default, **kwargs)


RECURSIVE = field("recursive", "包含子文件夹", "bool", True)
HEADER = field("header_row", "表头所在行", "int", 1, minimum=1, maximum=1000)
SHEET = field("sheet", "工作表名称", placeholder="留空读取默认工作表")

TOOLS = {
    "excel_merge": dict(title="表格合并与拆分", nav="表格合并拆分", group="表格处理", extensions=EXCEL_TYPES,
        description="按列名汇总多个表格，或按字段拆成多个文件，保留数据来源。",
        note="支持XLSX、XLSM、CSV、TSV。输出标准数据表；不保留宏、图形与原版式。公式读取已保存的计算结果，缺少缓存会报错。合并留空工作表时读取所有可用数据页。",
        fields=[field("action", "处理方式", "choice", "merge", choices=[("合并多个表格", "merge"), ("按字段拆分", "split")]),
                SHEET, HEADER, field("split_column", "拆分字段", placeholder="例如：部门", when=("action", ["split"])), RECURSIVE]),
    "excel_compare": dict(title="表格对账与差异", nav="表格对账", group="表格处理", extensions=EXCEL_TYPES,
        description="选择旧表和新表，按一个或多个主键核对新增、删除和字段变更。",
        note="队列第一份为旧表，第二份为新表，使用上移/下移调整。重复主键、空主键单独列为异常，不会强行匹配。可点击“查看列名”辅助填写。",
        fields=[field("key_columns", "主键列", "list", [], placeholder="例如：编号，姓名（多列用逗号分隔）"),
                field("sheet_left", "旧表工作表", placeholder="留空使用默认工作表"),
                field("sheet_right", "新表工作表", placeholder="留空使用默认工作表"), HEADER,
                field("ignore_whitespace", "比较时忽略首尾空白", "bool", True),
                field("numeric_tolerance", "数值允许误差", "float", 0, minimum=0, maximum=1e12, decimals=6)]),
    "excel_clean": dict(title="数据清洗与检查", nav="数据清洗检查", group="表格处理", extensions=EXCEL_TYPES,
        description="统一空白、日期和数值格式，识别重复记录与必填项缺漏。",
        note="列名可用逗号分隔。非法值保留并列入异常；编号列建议明确填写到“作为文本的列”，已被Excel舍入的长数字无法还原。输出数据页置于第一张。",
        fields=[SHEET, HEADER, field("trim_text", "清除文字首尾空白", "bool", True),
                field("remove_blank_rows", "删除整行空白", "bool", True), field("deduplicate", "去除重复记录", "bool", True),
                field("key_columns", "去重依据列", "list", [], placeholder="留空按整行判断"),
                field("required_columns", "必填列", "list", [], placeholder="例如：姓名，部门"),
                field("date_columns", "统一日期的列", "list", [], placeholder="例如：入职日期"),
                field("number_columns", "统一数值的列", "list", [], placeholder="例如：金额"),
                field("text_columns", "作为文本的列", "list", [], placeholder="例如：编号，身份证号"), RECURSIVE]),
    "template_fill": dict(title="模板批量生成文档", nav="模板批量填充", group="文档自动化", extensions=EXCEL_TYPES,
        description="一行数据生成一份Word或PPT，保留模板版式，批量填入姓名、部门等内容。",
        note="DOCX/PPTX模板使用 {{列名}}，例如 {{姓名}}。上方添加数据表，下方选择模板。只支持简单字段，不执行模板代码。文件名可用 {序号:03d} 和 {列名}。旧PPT请先另存为PPTX。",
        fields=[field("template_path", "文档模板", "file", "", filter="Word / PPT模板 (*.docx *.pptx)"), SHEET, HEADER,
                field("filename_template", "文件名规则", "text", "{序号:03d}", placeholder="例如：通知_{姓名}_{序号:03d}"),
                field("missing_policy", "缺字段时", "choice", "error", choices=[("记录错误并跳过该行", "error"), ("将缺失字段留空", "blank")]),
                field("output_pdf", "同时生成PDF（仅Word模板）", "bool", False), RECURSIVE]),
    "extract_fields": dict(title="文档表格与字段提取", nav="文档信息提取", group="文档自动化", extensions={".docx", ".pdf"},
        description="汇总多份Word、电子PDF中的表格与指定字段，标明来源位置。",
        note="字段规则可用正则，例如“姓名[：:]\\s*(\\S+)”；有捕获组时取第一组。未命中会提示，不会猜测内容。扫描图片中的文字需另行识别。",
        fields=[field("fields", "字段提取规则", "rules", [], columns=[("name", "字段名"), ("pattern", "匹配规则（正则）")]),
                field("include_tables", "同时提取可识别表格", "bool", True),
                field("per_file_first", "每个字段只取文件内首个命中", "bool", True),
                field("password", "PDF密码", "password", ""), RECURSIVE]),
    "document_export": dict(title="文档导出与图片提取", nav="文档导出提图", group="文档自动化", extensions={".docx", ".doc", ".pdf", ".txt"},
        description="把文档导出为纯文本，或批量取出Word中嵌入的原始图片。",
        note="纯文本支持Word、电子PDF和TXT；扫描PDF不识别文字。提图仅用于Word，保留原图格式；表格导出可使用“文档信息提取”。旧DOC由内置引擎转换后读取。",
        fields=[field("action", "处理方式", "choice", "text", choices=[("导出纯文本TXT", "text"), ("提取Word内嵌原图", "images")]),
                field("include_headers", "导出时包含页眉页脚", "bool", True, when=("action", ["text"])),
                field("password", "PDF密码", "password", "", when=("action", ["text"])), RECURSIVE]),
    "pdf_pages": dict(title="PDF页面整理", nav="PDF页面整理", group="文档自动化", extensions={".pdf"},
        description="按文件顺序合并PDF，或抽取、拆分和重新排列页面。",
        note="合并顺序由队列决定。页码从1开始，例如 1-3,5；倒序可写 8-6，重复页码会重复插入。原PDF保留。",
        fields=[field("action", "处理方式", "choice", "merge", choices=[("按队列顺序合并", "merge"), ("抽取指定页", "extract"), ("按页数拆分", "split"), ("重新排列页面", "reorder")]),
                field("pages", "页码或新顺序", "text", "1", when=("action", ["extract", "reorder"])),
                field("split_every", "每个文件页数", "int", 1, minimum=1, maximum=100000, when=("action", ["split"])),
                field("password", "PDF密码", "password", ""), RECURSIVE]),
    "image_pack": dict(title="图片压缩与合成PDF", nav="图片压缩合图", group="文档与图片", extensions=IMAGE_TYPES,
        description="批量调整图片体积与格式，或按顺序把多张图片合成PDF。",
        note="自动校正照片方向、清除EXIF，不放大小图；透明图片转JPG铺白底，多帧图只读首帧。设定KB上限后将尝试降低质量和尺寸，结果清单会说明是否达到目标。",
        fields=[field("action", "处理方式", "choice", "compress", choices=[("压缩 / 转换图片", "compress"), ("图片合成PDF", "pdf")]),
                field("output_format", "输出图片格式", "choice", "jpg", choices=[("JPG", "jpg"), ("PNG", "png"), ("WEBP", "webp"), ("保留原格式", "original")], when=("action", ["compress"])),
                field("max_width", "最大宽度（像素）", "int", 1600, minimum=1, maximum=30000),
                field("max_height", "最大高度（像素）", "int", 1600, minimum=1, maximum=30000),
                field("quality", "压缩质量", "int", 85, minimum=1, maximum=100),
                field("target_kb", "单张体积上限KB", "int", 0, minimum=0, maximum=1000000, when=("action", ["compress"])),
                field("pdf_page_size", "PDF页面", "choice", "A4", choices=[("A4纸张", "A4"), ("按图片尺寸", "image")], when=("action", ["pdf"])),
                field("pdf_landscape", "A4使用横向", "bool", False, when=("action", ["pdf"])), RECURSIVE]),
    "file_organize": dict(title="文件命名与归档", nav="文件命名归档", group="文件管理", extensions=None,
        description="先预览文件名与目标位置，再复制归档或执行可撤销的原地改名。",
        note="扩展名自动保留。支持 {序号:03d}、{原名}、{修改日期}、{父目录}。原地改名不移动目录，执行前校验文件，完成后保存撤销清单；源文件之后被修改时将拦截撤销。",
        fields=[field("action", "执行方式", "choice", "copy", choices=[("复制后命名归档（保留原件）", "copy"), ("原地重命名", "rename")]),
                field("filename_template", "文件名规则", "text", "{序号:03d}_{原名}"),
                field("group_by", "归档分类", "choice", "none", choices=[("不分类", "none"), ("按扩展名", "extension"), ("按修改年月", "month"), ("按文件名匹配分组", "pattern")]),
                field("group_pattern", "分组正则", placeholder="第一个捕获组作为分类名", when=("group_by", ["pattern"])), RECURSIVE]),
    "content_search": dict(title="文件夹全文检索", nav="文件全文检索", group="文件管理", extensions=TEXT_TYPES,
        description="检索多个文件或文件夹，在窗口左侧查看文件，右侧阅读命中段落。",
        note="支持DOCX、电子PDF、XLSX、CSV、TXT和HTML等。单击文件查看关键词及段落、页码或工作表行；不识别扫描图片。结果仅在窗口显示，最多5000条命中，达到上限会提示。",
        fields=[field("query", "搜索词（每行一个）", "multiline", "", placeholder="例如：项目名称\n合同编号"),
                field("match_mode", "匹配条件", "choice", "any", choices=[("包含任一搜索词", "any"), ("同时包含全部搜索词", "all")]),
                field("case_sensitive", "区分英文字母大小写", "bool", False),
                field("password", "PDF密码", "password", ""), RECURSIVE]),
    "find_duplicates": dict(title="重复文件查找", nav="重复文件查找", group="文件管理", extensions=None,
        description="在窗口中按组查看内容完全相同的文件，勾选后批量整理或移入回收站。",
        note="先按大小筛选，再校验文件内容；文件名不同也能识别。默认不勾选，每组至少保留一份；处理前展示确认清单，不覆盖已有文件。不支持永久删除。",
        fields=[RECURSIVE]),
    "check_materials": dict(title="材料完整性检查", nav="材料完整性", group="文件管理", extensions=None,
        description="按自定义材料清单检查缺件，并核对表格中的必填列和空值。",
        note="请添加待检查的文件夹。每个文件夹分别核对材料是否齐全；支持通配符，例如 通知*.pdf、附件/*.xlsx。必填列检查适用于表格文件。",
        fields=[field("required_files", "应有材料（每行一条）", "lines", [], placeholder="通知*.pdf\n名单.xlsx\n附件/*.docx"),
                field("required_columns", "表格必填列", "list", [], placeholder="例如：姓名，部门"), HEADER, RECURSIVE]),
}

EXTRA_ORDER = ["excel_merge", "excel_compare", "excel_clean", "template_fill", "extract_fields", "document_export", "pdf_pages", "image_pack", "file_organize", "content_search", "find_duplicates", "check_materials", "workflow"]
TOOLS["workflow"] = dict(title="组合流程", nav="组合流程", group="常用流程", extensions=None,
    description="把常用操作按顺序保存成流程，更换输入文件即可重复运行。",
    note="步骤只处理匹配的文件，其余文件保留给后续步骤。归档步骤使用副本；密码不会写入导出的配置。", fields=[])

WORKFLOW_CHOICES = [("表格合并与拆分", "excel_merge"), ("数据清洗与检查", "excel_clean"),
                    ("模板批量生成", "template_fill"), ("Word转PDF", "word_to_pdf"),
                    ("图片压缩或合成PDF", "image_pack"), ("PDF页面整理", "pdf_pages"),
                    ("文件复制归档", "file_organize"), ("Word章节合并与拆分", "word_chapters"),
                    ("文档导出与图片提取", "document_export"), ("表格打印排版与PDF", "excel_print"),
                    ("PDF压缩与标记", "pdf_finish"), ("报送材料装订", "submission_bind")]

TOOLS["word_to_pdf"] = dict(title="Word转PDF", nav="Word转PDF", group="流程步骤", extensions={".docx", ".doc", ".rtf"},
    note="使用随附的离线排版引擎，原文件保留。", description="", fields=[])

# Preserve existing mode positions and saved plans; new tools follow them.
from .feature_registry import SPECS as OFFICE_SPECS
TOOLS.update(OFFICE_SPECS)
EXTRA_ORDER.extend(OFFICE_SPECS)
