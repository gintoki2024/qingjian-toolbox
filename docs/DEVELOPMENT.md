# 开发与打包

## 运行

仓库根目录可直接运行 `启动青简工具箱.cmd`，首次按提示选择解释器与共享位置。也可执行：

```bat
python 程序文件\main.py
```

启动代码在导入 Qt 前自检，支持 64 位 Python 3.10–3.14，具体库兼容性以运行检查为准。未满足的依赖会按 requirements.txt 补入所选解释器，首次准备缺项需要联网。不要只复制 main.py，也不要删除小型 bootstrap_wheels。

环境只读检查：

```bat
启动青简工具箱.cmd --check-only
```

切换部署位置使用 `--choose-environment`；需要同步控制台排错使用 `--no-ui`。已完成准备的环境可用 `--offline --check-only` 检查离线复用。

## 修改与检查

源代码位于 `程序文件/qingjian`。环境启动逻辑与业务代码分开，约束见该目录的 AGENTS.md、开发说明和测试说明。按修改范围运行相应测试；界面与转换更新还应查看真实输出。

```bat
cd 程序文件
python -m pip install pytest
python -m pytest tests -q
```

仓库中的固定来源、大小和 SHA256 用于验证下载，不应无依据更改。requirements-lock.txt 保留已验证构建环境快照；源码自检依据 requirements.txt 的兼容范围，不能把构建工具误当运行时缺项全部补入。

## 生成下载包

在 `程序文件` 内运行 `python package_source.py --help` 或 `python build_cmd.py --help` 查看当前参数。轻量包使用 `--entry-mode cmd`；完整包通过 build_cmd.py 收集可移动解释器、依赖与已验证的本地引擎。旧 EXE 构建源码仅作历史保留，不作为本次发布入口。

用户下载包保持“启动CMD＋使用说明＋程序文件”三项根目录。GitHub 仓库额外带 README、许可和开发资料。大型运行时、实际个人配置和构建缓存不得提交 Git；完整包和源码 ZIP 放在 Releases 附件。

本仓库通过 `.gitattributes` 保留文件原始字节，避免 CMD 及历史构建源码的行尾被 Git 自动改写，导致固定哈希检查失效。

## v0.10.3 公开副本验证范围

本次沿用已验证的 v0.10.3 CMD 完整包，以及源码共享环境修正版。此前完成 248 项相关回归和 57 项功能自检。公开副本只调整 ZIP 包装、根使用说明、许可导航，并删除完整包来源记录中的构建机路径；程序逻辑与业务 UI 不变。

公开整理另核验 ZIP 成员内容、CRC、三项结构、源码与仓库的一致性以及完整包和源码业务文件的一致性。此前测试不代表在每一台办公电脑上均已验证；现场限制以实际运行结果为准。
