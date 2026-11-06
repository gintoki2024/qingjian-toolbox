# Mac 构建与验收

轻量源码 ZIP 可以在 Windows 上生成；完整运行包必须在真实 macOS 上准备、构建并验收。两种发行包解压后均只有启动器、使用说明、程序文件三项。源码共用两种架构的下载清单；完整包按 arm64、x86_64 分开构建。

`package_macos.py` 只打包，不下载。`build_macos.py` 拒绝 Windows 或其他架构的 Python，拒绝系统 Python，只复制带固定来源收据的可移动 Python 与经过收据验证的原生组件；复制后检查包内链接、Mach-O 架构及动态库的绝对引用。复制操作保留 Mac 框架内部的相对链接和可执行权限。

在对应架构 Mac 中执行：

```bash
python3 macos_build/ci_build.py --home '/自选磁盘/青简共享环境' --output '/自选磁盘/青简Mac构建'
```

该脚本先调用环境准备，再使用已下载的本地资源构建，最后完成包内 Python 隔离导入、两次启动自检、Cocoa 原生界面与 27 个工具页面、记事双窗及分钟输入、实际离线业务检查。Python 网络连接在业务与界面验收进程中被禁止。在线网页抓取不作为离线转换项目测试。

`verify_macos.py` 通过后才写入 `macos-build-validation.json`，`package_full()` 默认要求此验收记录；未验证的目录不能当成完整包发布。原生 UI 检查要求真实 Cocoa 平台，不把 offscreen 截图当成 Mac 原生验收。

`workflows/build-macos.yml` 是手动构建模板，当前目录不是 GitHub 工作流目录。获准使用云端构建后再放入仓库 `.github/workflows/`。模板只支持手动触发，默认先构建 Apple 芯片，使用标准 Mac runner、35 分钟单次上限、串行构建和 3 天产物保留，不创建 GitHub Release、不更改仓库可见性。模板不是计费预算设置；运行会使用私有仓库所属账号的 Actions 额度，实际费用由账号的额度和预算设置决定。

完整包的签名和办公电脑安全策略需要单独按组织要求处理；工具不改执行策略、权限、Gatekeeper 或安全软件配置。
