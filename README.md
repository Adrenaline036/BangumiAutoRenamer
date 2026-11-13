# BangumiAutoRenamer

BangumiAutoRenamer 是一个影视整理工具。通过 Bangumi、TMDB 查找作品，确认作品与季度、编辑文件内容、审核计划，再执行保留源文件的硬链接整理。WebUI 使用 NiceGUI，支持动画、真人剧集和电影。

当前版本：**0.3.0-rc.83**。运行环境以 **Python 3.11** 为验证基准。

## 功能

- 添加来源目录，查看后台队列、待处理任务和历史记录。
- 选择作品、季度和剧集范围，人工输入作为后续计划的依据。
- 在计划中纳入或排除文件，检查正片、特别篇、字幕及配套内容。
- 审核目标路径、冲突与提示，批准后执行硬链接；源下载文件保持原位。
- 查看媒体库登记和恢复记录，在原任务上继续处理待确认或失败事项。

## Windows 启动

取得本版本源码后，在仓库根目录打开 PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 -m src.start
```

浏览器打开 `http://127.0.0.1:5999`。首次使用先打开“配置”，填写 TMDB API 密钥和目标目录；Bangumi Token 可按需要配置。

1. 点击“添加任务”，选择来源目录与媒体类型。
2. 等待队列读取，确认作品和季度；资料不足时在原任务中补充。
3. 核对计划中的文件、名称、目标路径、排除项及冲突提示。
4. 批准计划后确认执行，并查看任务结果。

当前计划执行使用“链接”模式。来源与目标需要位于支持硬链接的同一文件系统；配置页的旧“复制/剪切”选项不代表当前已批准计划支持这两种执行方式。

## 文档

- [使用说明](docs/使用说明.md)：配置、队列、审核、恢复、Webhook与容器运行。
- [开发与验证](docs/开发与验证.md)：依赖入口、测试范围、隔离数据和源码指纹。
- [发布检查](docs/发布检查.md)：公开文件范围、隐私核查及版本检查。
- [版本说明](CHANGELOG.md)。

## 数据与访问

默认运行数据保存在 `data/`，包括配置、任务、计划和恢复记录。可以通过 `BAR_DATA_DIR` 指定独立目录；切换数据目录会看到另一套任务，不会自动迁移原记录。

WebUI和`/sendTask`用于可信本机或局域网环境，没有内置账号登录。不要直接暴露到公网。提交问题时提供脱敏的操作步骤和错误片段，移除API密钥、Token、私有地址、实际媒体路径和任务数据。

## 许可证与来源

采用 [GPL-3.0](LICENSE)。项目源自 [KimigaiiWuyi/Bangumi_Auto_Rename](https://github.com/KimigaiiWuyi/Bangumi_Auto_Rename)，保留原作者署名。使用 Bangumi、TMDB 数据时遵守相应服务的使用条件。
