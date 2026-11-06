"""Explicit registrations for the model-free office extension tools."""
from .core import document_review, excel_advanced, office_prepare, pdf_office

MODULES = (document_review, excel_advanced, office_prepare, pdf_office)
SPECS, GUIDES, OWNERS = {}, {}, {}
for module in MODULES:
    for mode, spec in module.TOOL_SPECS.items():
        if mode in SPECS:
            raise RuntimeError("工具名称重复：" + mode)
        SPECS[mode] = spec
        OWNERS[mode] = module
    GUIDES.update(module.GUIDES)


def run(mode, sources, folder, options, progress=None, cancel=None):
    return OWNERS[mode].run(mode, sources, folder, options, progress, cancel)


def validate(mode, options, sources=None):
    return OWNERS[mode].validate(mode, options, sources)


def build_example(mode, folder):
    return OWNERS[mode].build_example(mode, folder)
