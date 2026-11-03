# 第三方组件与许可位置

本项目自有代码沿用 MIT；第三方组件保留各自版权与许可，不能将完整整合包概括为统一 MIT。原有明细见 `程序文件/第三方组件说明.txt`、各运行库目录及 `程序文件/support`。

## PySide6 / Qt / Shiboken

v0.10.3 完整包内 PySide6-Essentials 与 Shiboken6 为 6.12.0，其包 METADATA 声明 `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`。本项目使用开放许可组件，保留可替换的 Python 包、DLL 和插件，不修改其上游代码。各 Qt 内含第三方代码仍按各自许可。

- LGPLv3 与 GPLv3 全文副本：`程序文件/public-notices/LGPL-3.0.txt` 与 `GPL-3.0.txt`，从原完整包已带的许可证逐字复制。
- 完整包原有 LGPLv3 文本还可见 `程序文件/licenses/RTFDE/rtfde-0.1.2.2.dist-info/licenses/LICENSE`、LibreOffice 的 `license.txt` 等；此处列出直接入口方便查找，不改变组件许可。
- PySide/Shiboken 源码：[官方 pyside-setup 仓库](https://code.qt.io/cgit/pyside/pyside-setup.git/)，选择与所带版本一致的发布标签；Qt 源码：[官方 Qt 发布归档](https://download.qt.io/official_releases/qt/)。源码包实际安装版本可能随兼容要求变化，应以所选解释器中的包版本为准。
- 上游许可说明：[Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html)、[Qt 开源许可](https://www.qt.io/licensing/open-source-lgpl-obligations)。

本项目源码和重建入口已公开，替换 Qt/PySide 或修改本项目后可自行运行、调试与重建；保持本项目所用 API 和二进制兼容要求。

## 其他主要组件

| 组件 | 原有许可 / 来源材料位置（完整包内） |
| --- | --- |
| Python | `程序文件/Python/LICENSE.txt`；[上游源码](https://github.com/python/cpython) |
| LibreOffice | `程序文件/engines/LibreOffice/license.txt`；[对应发布源码目录](https://download.documentfoundation.org/libreoffice/src/) |
| extract-msg | `程序文件/Python/Lib/site-packages/extract_msg-0.58.1.dist-info/LICENSE.txt`（GPLv3）；[源码](https://github.com/TeamMsgExtractor/msg-extractor) |
| Ghostscript | `程序文件/support/Ghostscript/licenses/COPYING`（AGPLv3）；对应源码归档保存在 `support/Ghostscript/sources` |
| 7-Zip | `程序文件/support/7zip/License.txt`（含 LGPL / unRAR 等说明）；对应源码归档保存在 `support/7zip/sources` |
| OFDRW 与桥依赖 | `程序文件/support/OFD` 中的许可证、`source-origins.json`、桥源码与重建说明 |
| Eclipse Temurin Java | `程序文件/support/OFD/runtime/legal`；对应源码归档保存在 `support/OFD/sources` |
| 其他 Python 库 | `程序文件/licenses` 及各包 dist-info 的 license/metadata；构建版本见 `程序文件/licenses/组件版本.json` |

完整包的 `support/component-checksums.json` 保存对应原生组件校验清单。轻量源码不携带大型引擎或大型上游源码归档，保留来源、校验、小型许可和源码获取说明；补齐逻辑见 `native-sources.json`、`runtime-sources.json` 和 `bootstrap-wheels.json`。

分发副本或修改组件时，请保留适用的版权、许可与源码获取安排；本说明是现有材料的导航，不替代各组件的完整许可文本。
