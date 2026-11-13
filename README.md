# 青简工具箱

面向日常职场办公的本地工具合集，使用 Python + Qt，提供独立中文界面。重点是公文批量处理、材料核查与拖拽转换。

<!-- UNIFIED_DOWNLOADS -->
## 按电脑系统下载

**[进入 Windows / macOS 统一下载页](https://github.com/gintoki2024/qingjian-toolbox/releases/tag/downloads-2026-11-12)**

| 电脑系统 | 完整运行包 | 轻量源码包 |
| --- | --- | --- |
| Windows 10 / 11，64 位 · v0.11.4 | [Windows 完整包](https://github.com/gintoki2024/qingjian-toolbox/releases/download/downloads-2026-11-12/QingjianToolbox-v0.11.4-Windows-x64-Full.zip) | [Windows 源码](https://github.com/gintoki2024/qingjian-toolbox/releases/download/downloads-2026-11-12/QingjianToolbox-v0.11.4-Source.zip) |
| Mac，Apple 芯片（M 系列） · v0.12.1 试用版 | [Apple 芯片完整包](https://github.com/gintoki2024/qingjian-toolbox/releases/download/downloads-2026-11-12/QingjianToolbox-v0.12.1-macOS-arm64-Full.zip) | [Mac 通用源码](https://github.com/gintoki2024/qingjian-toolbox/releases/download/downloads-2026-11-12/QingjianToolbox-v0.12.1-macOS-Source.zip) |
| Mac，Intel 芯片 · v0.12.1 试用版 | [Intel 完整包](https://github.com/gintoki2024/qingjian-toolbox/releases/download/downloads-2026-11-12/QingjianToolbox-v0.12.1-macOS-x86_64-Full.zip) | [Mac 通用源码](https://github.com/gintoki2024/qingjian-toolbox/releases/download/downloads-2026-11-12/QingjianToolbox-v0.12.1-macOS-Source.zip) |

本轮两个系统的完整运行包和源码包均已同步转换改良；Windows 与 Mac 的版本号分别维护。完整包内置 Python 和必要组件；源码首次启动自检并只补缺项，已有共享环境继续复用。

Mac 仍为未签名、未公证的试用版，要求 macOS 14.4 及以上。请直接在 Mac 上解压；芯片类型在苹果菜单「关于本机」查看。启动与校验说明见统一下载页。

<!-- /UNIFIED_DOWNLOADS -->

## 本轮更新：Windows v0.11.4 / macOS v0.12.1

- 转换轮盘每页最多六项：不超过六项同页显示，超过六项排满后分页，常用格式优先；TXT 五个目标同页，保留拖住文件滚轮翻页。
- 移除 PDF 转 Excel/CSV；PDF 转 Word、提取文字面向含文字的电子 PDF，扫描件不会自动识别文字。
- 普通 Excel/CSV/TSV 转换支持指定工作表或多表分别导出，独立设置读取/输出编码和分隔符，保留前导零与长编号；没有公式计算缓存时明确提示。
- 图片质量、尺寸、背景等适用参数可设置并记忆；多图合并支持调整顺序，多帧转静态图片逐帧输出。
- 转换结果支持逐个打开多输出、定位文件、按原选项重试失败项；原件与已有结果不覆盖。
- 保留环境自检、共享解释器、包外共享组件及国内备用来源，不因更换源码目录重装已就绪环境。
- 修复 Mac 文档导出 PDF 时中文缺字及新生成文档标题复制顺序异常的问题。

![TXT 五种转换格式同页显示](docs/images/converter-txt.png)

Windows 的源码包和完整运行包均为 v0.11.4；Mac 的 Apple / Intel 完整包和通用源码包均为 v0.12.1 试用版。各包解压后根目录仍只有启动器、使用说明、程序文件三项。

[Windows 详细发布记录](https://github.com/gintoki2024/qingjian-toolbox/releases/tag/v0.11.4) · [macOS 详细发布记录](https://github.com/gintoki2024/qingjian-toolbox/releases/tag/v0.12.1-macOS) · [历史版本](https://github.com/gintoki2024/qingjian-toolbox/releases)

Mac 完整包在对应架构的 macOS 构建并执行自动验收；Finder 拖住文件滚轮、多屏吸附和通知体验仍需实际桌面反馈。Mac 字体与 Windows 不同，复杂公文排版请查看输出成品。

## 可以做什么

| 分组 | 主要功能 |
| --- | --- |
| 文档处理 | 公文格式整理与检查、Word 批量格式调整、内容删除/替换、多组自定义脱敏规则与记忆库、Word/PPT 模板批量填充、章节合拆、版本比对 |
| 表格与核查 | 表格对账、数据清洗、材料完整性、跨材料一致性、数字勾稽、多表匹配回填、报送前隐藏信息检查 |
| PDF 与图片 | Word/PDF 等本地转换、PDF 合并拆分、页码水印、压缩标记、图片裁剪与格式转换、报送材料装订 |
| 检索与查重 | 检索结果直接在窗口列表展示文件名和路径，单击查看命中段落；重复文件按组展示，可批量移到整理目录或系统回收站，每组至少保留一份 |
| 文件管理与组合流程 | 批量命名、整理，以及按顺序保存和复用多步骤处理 |
| 独立转换浮窗 | 拖入文件选择目标，拖住文件时可滚轮翻页；本地结果保存在原文件夹，重名自动编号；详见[转换格式说明](程序文件/转换格式说明.txt) |
| 记事与提醒 | 可拆分日历/事项小窗、日期与小时分钟预约、定期提醒、重要程度颜色、置顶与玻璃视觉 |

首页上方是工具分组，下方通过按钮切换图片、文字或待办事项，一次显示一种。外观设置可调整全局 50%—100% 不透明度及窗口吸附；拖动时按住 `Alt` 可临时自由移动。复杂工具页提供中文说明与虚构示例。

组合流程当前支持：表格合并与拆分、数据清洗与检查、模板批量生成、Word 转 PDF、图片压缩或合成 PDF、PDF 页面整理、文件复制归档、Word 章节合并与拆分、文档导出与图片提取、表格打印排版与 PDF、PDF 压缩与标记、报送材料装订。需区分两组输入或需要交互确认的对账、核查、检索和查重保留独立入口；可在流程页查看“步骤范围”。

## Windows 开始使用

1. 下载一种命名的 ZIP，**完整解压**到可写文件夹。
2. 打开解压后的文件夹，双击 `启动青简工具箱.cmd`。不要在压缩软件内直接启动，也不要只复制启动器。
3. 从首页选择工具，添加文件、核对参数后处理；快速转换与记事有独立浮窗入口。

两种下载包解压后的根目录均恰为三项：

```text
启动青简工具箱.cmd
使用说明.txt
程序文件/
```

完整包直接使用包内环境，初始化及本地文件处理可离线完成。源码包第一次启动可选择复用本机 Python，或将备用 Python 部署到所选盘。复用模式只把缺失或不兼容的 Python 库补入所选解释器；LibreOffice 等独立引擎、缓存和日志使用所选共享目录。默认建议包外同盘的“青简共享环境”，也可选择其他绝对路径。环境齐全后直接复用，不重复下载；下载使用 HTTPS 和固定组件校验，并保留官方及国内备用来源。

更新源码时保留包外共享环境，新版会记住并校验已有位置。若旧版组件仍在旧源码文件夹中，先保留旧包，在旁边启动新版一次，完成校验迁移后再删除旧源码。已删除、损坏或不兼容的组件需要重新补齐。需要重新选择位置时运行：

```bat
启动青简工具箱.cmd --choose-environment
```

普通源码启动完成准备后，CMD 自动结束，主界面及已启动的小窗独立运行。同步查看启动诊断可使用 `--no-ui`。

## 本地处理与使用边界

- 本地文档、表格、图片处理不需要 GPU 或本地模型。本版不包含 OCR、录音转写等模型功能；网页链接转 Word 及下载网页内容仍需访问对应网站。
- 文档在本机处理，记事与规则保存在用户数据目录；发行包和仓库不携带真实办公材料、个人事项、规则库或部署选择。
- 记事提醒需要程序或托盘进程保持运行；关机、休眠或完全退出后不会弹出提醒。
- PDF 转 Word 主要面向电子 PDF 的文字与基本结构。复杂版式、公式、图表、扫描件及字体替换效果须人工查看输出，各转换方向的边界以程序提示和格式说明为准。
- CMD 是启动入口，包内 Python、Java 等上游组件仍含 EXE/DLL。Windows 或单位终端策略可能限制运行，是否可用须以现场验证为准。
- 查重默认不勾选文件，批量处理前会显示确认清单；系统回收站失败时不会改为永久删除。处理前仍建议确认文件用途及保留副本。

## 源码开发与许可

仓库包含程序源码、启动准备代码、固定来源和校验清单、小型许可材料及必要引导 wheel，不含大型引擎。可双击根 CMD，或在可用 Python 下执行 `python 程序文件\main.py`，同样先检查环境。开发与打包见[开发说明](docs/DEVELOPMENT.md)，功能操作见[程序内说明](程序文件/使用说明.txt)。

本项目自有代码沿用 [MIT](LICENSE)。第三方组件分别适用各自许可，包括 LGPL/GPL/AGPL 等；MIT 不覆盖整个整合包。版本、来源、源码及许可位置见[第三方说明](THIRD_PARTY_NOTICES.md)和包内材料。

浮动转换的交互思路参考 [Filee](https://github.com/WDHUS4G/Filee)，本工具使用独立 Python/Qt 实现。
