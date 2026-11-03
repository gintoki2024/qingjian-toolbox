"""Dispatch for aggregate tools and composable local workflows."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from .catalog import TOOLS, WORKFLOW_CHOICES
from .core.common import (CancelledError, atomic_write, ensure_cancel, expand_inputs,
                          make_result, notify, safe_name, unique_path)


def scrub_secrets(value):
    if isinstance(value, dict):
        return {key: ("" if "password" in key.lower() else scrub_secrets(item))
                for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    return value


def validate_tool_options(mode, options, sources=None):
    if not isinstance(options, dict):
        raise ValueError("工具设置格式无效。")
    if mode == "workflow":
        steps = options.get("steps", [])
        allowed = {key for _, key in WORKFLOW_CHOICES}
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError("请添加1至20个流程步骤。")
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or step.get("tool") not in allowed:
                raise ValueError(f"第{index}步的工具无效。")
            validate_tool_options(step["tool"], step.get("options", {}))
            if step["tool"] == "file_organize" and step.get("options", {}).get("action", "copy") != "copy":
                raise ValueError("组合流程的文件归档使用副本，请选择复制方式。")
        return
    if mode not in TOOLS:
        if mode == "undo_rename":
            return
        raise ValueError("未知的工具。")
    from .feature_registry import SPECS as OFFICE_SPECS, validate as validate_office
    if mode in OFFICE_SPECS:
        validate_office(mode, options, sources)
    for item in TOOLS[mode]["fields"]:
        if item["key"] not in options:
            continue
        value, kind = options[item["key"]], item["kind"]
        if kind in {"list", "lines", "rules"} and not isinstance(value, list):
            raise ValueError(item["title"] + "的格式应为列表。")
        if kind == "choice" and value not in [choice for _, choice in item["choices"]]:
            raise ValueError(item["title"] + "包含未知选项。")
    if mode == "excel_merge" and options.get("action") == "split" and not options.get("split_column", "").strip():
        raise ValueError("请填写拆分字段。")
    if mode == "excel_compare" and not options.get("key_columns"):
        raise ValueError("请填写用于匹配两份表格的主键列。")
    if mode == "template_fill":
        if not Path(options.get("template_path", "")).is_file():
            raise ValueError("请选择有效的DOCX或PPTX模板。")
        if Path(options["template_path"]).suffix.lower() not in {".docx", ".pptx"}:
            raise ValueError("模板支持DOCX或PPTX；旧PPT请先另存为PPTX。")
        if not options.get("filename_template", "").strip():
            raise ValueError("请填写生成文档的文件名规则。")
    if mode == "content_search" and not options.get("query", "").strip():
        raise ValueError("请填写至少一个搜索词。")
    if mode == "check_materials" and not (options.get("required_files") or options.get("required_columns")):
        raise ValueError("请填写应有材料清单或需要检查的必填列。")
    if mode == "check_materials" and sources and any(not Path(path).is_dir() for path in sources):
        raise ValueError("材料完整性检查请添加文件夹，每个文件夹会分别检查。")
    if mode == "extract_fields":
        if not options.get("fields") and not options.get("include_tables", True):
            raise ValueError("请添加提取字段，或勾选提取表格。")
        for field in options.get("fields", []):
            if not isinstance(field, dict) or not field.get("name", "").strip() or not field.get("pattern", ""):
                raise ValueError("提取规则需要填写字段名和匹配规则。")
            re.compile(field["pattern"])
    if mode == "file_organize":
        if not options.get("filename_template", "").strip():
            raise ValueError("请填写文件名规则。")
        if options.get("action") == "rename" and options.get("group_by", "none") != "none":
            raise ValueError("原地改名不移动文件夹，请将归档分类设为不分类。")


def execute_tool(mode, sources, folder, options, progress=None, cancel=None):
    ensure_cancel(cancel)
    sources = [Path(p) for p in sources]
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    validate_tool_options(mode, options, sources)
    from .feature_registry import SPECS as OFFICE_SPECS, run as run_office
    if mode in OFFICE_SPECS:
        return run_office(mode, sources, folder, options, progress, cancel)
    if mode in {"excel_merge", "excel_clean", "excel_compare"}:
        from .core.excel_ops import run_excel
        return run_excel(mode, sources, folder, options, progress, cancel)
    if mode in {"template_fill", "pdf_pages", "image_pack"}:
        from .core.document_ops import run_documents
        return run_documents(mode, sources, folder, options, progress, cancel)
    if mode in {"file_organize", "find_duplicates", "check_materials"}:
        from .core.file_ops import run_files
        return run_files(mode, sources, folder, options, progress, cancel)
    if mode in {"content_search", "extract_fields"}:
        from .core.content_ops import run_content
        return run_content(mode, sources, folder, options, progress, cancel)
    if mode == "document_export":
        from .core.document_export import run_document_export
        return run_document_export(sources, folder, options, progress, cancel)
    if mode == "undo_rename":
        from .core.file_ops import undo_organize
        return undo_organize(sources[0], folder, progress, cancel)
    if mode == "workflow":
        return run_workflow(sources, folder, options, progress, cancel)
    if mode == "word_to_pdf":
        from .core.conversion import convert_word
        files = expand_inputs(sources, TOOLS[mode]["extensions"], exclude_root=folder)
        if not files:
            raise ValueError("没有可转换的Word文件。")
        outputs, errors, warnings = [], [], []
        for index, path in enumerate(files, 1):
            ensure_cancel(cancel)
            notify(progress, index - 1, len(files), path.name)
            try:
                target = unique_path(folder, path.stem + ".pdf")
                detail = convert_word(path, target, options)
                outputs.append(str(target))
                warnings.extend(detail.get("warnings", []))
            except CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{path.name}：{exc}")
            notify(progress, index, len(files), path.name)
        result = make_result(outputs, len(outputs), warnings, errors)
        result["data_outputs"] = outputs.copy()
        return result
    raise ValueError("未知的工具类型。")


def run_workflow(sources, folder, options, progress=None, cancel=None):
    validate_tool_options("workflow", options)
    current = expand_inputs(sources, None, True, folder)
    if not current:
        raise ValueError("没有可用于流程的输入文件。")
    outputs, warnings, errors, stages = [], [], [], []
    steps = options["steps"]
    stopped, cancelled = False, False
    for index, step in enumerate(steps):
        mode, step_options = step["tool"], dict(step.get("options", {}))
        suffixes = TOOLS[mode]["extensions"]
        eligible = [p for p in current if suffixes is None or p.suffix.lower() in suffixes]
        carried = [p for p in current if p not in eligible]
        destination = folder / f"{index + 1:02d}_{safe_name(TOOLS[mode]['title'])}"
        def step_progress(done, total, message=""):
            percent = (index + min(1, max(0, done / max(1, total)))) / len(steps)
            notify(progress, round(percent * 1000), 1000, f"步骤{index + 1}/{len(steps)} · {message}")
        try:
            ensure_cancel(cancel)
            if not eligible:
                raise ValueError(f"没有匹配“{TOOLS[mode]['title']}”格式的输入文件。")
            destination.mkdir()
            if mode == "file_organize":
                from .core.file_ops import plan_organize
                step_options["action"] = "copy"
                step_options["_plan"] = plan_organize(eligible, destination, step_options, step_progress, cancel)
            result = execute_tool(mode, eligible, destination, step_options, step_progress, cancel)
        except CancelledError:
            cancelled = True
            warnings.append(f"步骤{index + 1}已停止；该步骤内已完成的文件保留在结果目录。")
            outputs.extend(str(p) for p in destination.rglob("*") if p.is_file()
                           and not any(part.startswith(".") for part in p.relative_to(destination).parts))
            stages.append({"步骤": index + 1, "工具": TOOLS[mode]["title"], "输入数": len(eligible), "输出数": 0, "异常数": 0, "状态": "已取消"})
            break
        except Exception as exc:
            errors.append(f"步骤{index + 1}：{exc}")
            outputs.extend(str(p) for p in destination.rglob("*") if p.is_file()
                           and not any(part.startswith(".") for part in p.relative_to(destination).parts))
            stages.append({"步骤": index + 1, "工具": TOOLS[mode]["title"], "输入数": len(eligible), "输出数": 0, "异常数": 1, "状态": "失败"})
            stopped = True
            warnings.append("当前步骤无法完成，已停止流程；此前产物保留。")
            break
        produced = [Path(p) for p in result.get("data_outputs", result.get("outputs", []))]
        outputs.extend(result.get("outputs", []))
        warnings.extend(f"步骤{index + 1}：{s}" for s in result.get("warnings", []))
        stage_errors = result.get("errors", [])
        errors.extend(f"步骤{index + 1}：{s}" for s in stage_errors)
        current = produced + carried
        cancelled = bool(result.get("cancelled")) or bool(cancel and cancel())
        stages.append({"步骤": index + 1, "工具": TOOLS[mode]["title"], "输入数": len(eligible),
                       "输出数": len(produced), "异常数": len(stage_errors),
                       "状态": "已取消" if cancelled else ("有异常" if stage_errors else "完成")})
        if cancelled:
            warnings.append("流程已停止，已完成的产物保留。")
            break
        if stage_errors and options.get("stop_on_error", True):
            stopped = True
            warnings.append(f"步骤{index + 1}出现异常，后续步骤未执行；中间结果已保留。")
            break
        if not produced:
            errors.append(f"第{index + 1}步没有生成可供后续步骤使用的文件，请查看该步骤的处理报告。")
            stopped = True
            break
        notify(progress, index + 1, len(steps), f"已完成步骤{index + 1}")
    cancelled = cancelled or bool(cancel and cancel())
    if options.get("archive", True) and errors:
        warnings.append("流程存在异常，未生成最终ZIP；各步完整产物已保留，请复核后重新归档。")
    if options.get("archive", True) and not errors and not stopped and not cancelled:
        archive = unique_path(folder, "最终文件.zip")
        def write_archive(path):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as stream:
                seen = set()
                for index, file in enumerate(current, 1):
                    ensure_cancel(cancel)
                    if not file.is_file():
                        raise FileNotFoundError(f"待归档文件不存在：{file.name}")
                    try:
                        relative = file.resolve().relative_to(folder.resolve())
                    except ValueError:
                        relative = Path("未处理附件") / f"{index:03d}_{file.name}"
                    key = str(relative).casefold()
                    if key in seen:
                        relative = Path("同名附件") / f"{index:03d}_{file.name}"
                    seen.add(str(relative).casefold())
                    stream.write(file, relative)
        try:
            atomic_write(archive, write_archive)
            outputs.append(str(archive))
        except CancelledError:
            cancelled = True
            warnings.append("已停止归档，未发布不完整ZIP；各步产物保留。")
        except Exception as exc:
            errors.append(f"最终归档失败：{exc}")
    report = unique_path(folder, "流程记录.json")
    atomic_write(report, lambda p: p.write_text(json.dumps({"状态": "已取消" if cancelled else ("有异常" if errors else "完成"),
                 "步骤": stages, "提示": warnings, "异常": errors}, ensure_ascii=False, indent=2), encoding="utf-8"))
    outputs.append(str(report))
    result = make_result(list(dict.fromkeys(outputs)), sum(s["输出数"] for s in stages), warnings, errors)
    result["data_outputs"] = [str(p) for p in current]
    result["cancelled"] = cancelled
    return result
