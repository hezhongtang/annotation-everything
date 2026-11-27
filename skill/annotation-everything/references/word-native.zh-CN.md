# 在 Word 中连接审阅

此流程使用已保存到本机的 DOCX、桌面 Microsoft Word、WordApi 1.6，以及该审阅正在运行的浏览器对照页。macOS 26.7.3 上已用 Word 16.115.2 验证开发侧载。Windows 的启动代码和操作步骤可供测试，真机验收待完成。请同时交给审阅人该份审阅的浏览器地址，受保护或带签名的文件可在该地址审阅。下文的 `$S` 指安装技能的 `scripts` 目录。

## 首次启用

- **macOS 开发侧载。** 在 Finder 的“前往文件夹”中打开 `~/Library/Containers/com.microsoft.Word/Data/Documents/`，按需新建 `wef`。把技能目录下的 `word-taskpane/manifest.xml` 复制进去。终端写入容器遇到权限提示时，可以直接用 Finder。保存现有工作，完全退出 Word，待下方命令重新打开原件后，在“开始 → 加载项 → Developer Add-ins”选择“AE 本机审阅”。[Microsoft 的 macOS 侧载说明](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/sideload-an-office-add-in-on-mac)
- **Windows 开发侧载（待真机验证）。** 在有访问权限的测试共享目录中只放 `manifest.xml`。在 Word 的“文件 → 选项 → 信任中心 → 信任中心设置 → 受信任的加载项目录”添加该共享的 UNC 地址，勾选“显示在菜单中”。保存工作并重开 Word，在“开始 → 加载项 → 高级 → 共享文件夹”选择“AE 本机审阅”并添加。文档本身保存在本机盘符路径下。组织禁用共享目录或环回访问时，请管理员评估策略。[Microsoft 的 Windows 侧载说明](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/create-a-network-shared-folder-catalog-for-task-pane-and-content-add-ins)
- **证书。** 为本机服务准备受该设备信任、SAN 含 IP 地址 `127.0.0.1` 的服务证书和配对 PEM 私钥，私钥仅留本机。macOS 实测证书为一天有效的自签名叶证书，通过当前用户登录钥匙串为该 IP 添加 SSL 信任。正式设备可向管理员索取证书并确认用户信任策略。

## 启动或重新连接

1. **选择审阅。** 新建时运行 `python3 "$S/build_viewer.py" --report "$DOCX" --out "$REVIEW" --name "$NAME" --locale zh-CN`。已有审阅用 `python3 "$S/serve.py" list --json` 对照审阅名、标识及目录，并核对登记的报告源文件。沿用原审阅目录，保留既有批次和批注。两份同名合同分别核对目录和文内标识。
2. **确认接收方。** 按[接收方绑定与交接](receiver-binding.zh-CN.md)检查当前会话，启动或复用 `python3 "$S/serve.py" "$REVIEW" --no-open`，记录其浏览器地址。旧审阅可以先查看，侧栏会显示“接收方未绑定”。只有向会话交件时才需要接收方。更换会话要显式改绑，再重启该审阅的专属服务。
3. **启动窗格服务并打开原件。** `--open-word` 核对已登记的文件、指纹和专属审阅后交给 Microsoft Word 打开。在可保持运行的终端执行下方命令。每次启动会产生新绑定码；直接从该终端交给审阅人，不写进审阅材料或提交记录。

   ```sh
   python3 "$S/word_taskpane.py" "$REVIEW" --document "$DOCX" --cert "$CERT" --key "$KEY" --open-word
   ```

   Windows PowerShell 在设置相同的路径变量后运行 `py -3 "$S/word_taskpane.py" "$REVIEW" --document "$DOCX" --cert "$CERT" --key "$KEY" --open-word`。要自行用 Word 打开原件，可省略 `--open-word`。

4. **展开侧栏。** 在 Word 的“开始”点击“打开审阅”。需要重新加载时，在窗格菜单选择“Reload”。打开文档与展开窗格是两个独立的动作。微软支持在指定任务窗格、文档存有自动展开设置、加载项已安装且分发渠道支持时自动展开。AE 使用功能区按钮，交付文档不添加该设置；收件人可以照常在 Word 打开文档。[自动展开条件](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/automatically-open-a-task-pane-with-a-document)
5. **在窗格核对。** 输入绑定码，点“绑定当前文档”，核对文件位置、审阅名与标识、批次和接收会话。点“刷新连接状态”可读取最新批次。出现新的跟踪修订需要审阅人明确应用建议；打开或重连只读取信息。

## 保存、另存和停止后

- **保存后重新打开同一原件。** 用这份审阅的绑定码重新连接。若出现“文档需核对”（Word 自己保存也会改变包指纹），先核对完整路径与文内标识，关闭窗格服务，然后显式运行 `python3 "$S/build_viewer.py" --rebuild "$REVIEW"`。查看重建报告，核对内容已变或无法定位的批注。在同一专属审阅运行时重新启动窗格服务，输入新绑定码。重建沿用审阅标识，按段落标识与文本摘要重新定位。
- **另存为副本或切换同名文档。** 选择“重新选择审阅”。回到已绑定的原件，或用副本的完整路径和独立目录创建或打开它自己的审阅。环回窗格来源一次连接一份审阅。要切换到另一份，先用 Ctrl+C 关掉自己启动的窗格服务，再以另一目录及源文件启动，核对新的绑定码。各审阅的浏览器专属服务可以同时运行。
- **服务停止、端口易主、接收方改绑。** 停止后按原审阅标识重新启动专属服务。按新身份重启窗格服务，重新输入绑定码。服务无法连接、宿主不支持或文件超出 16 MiB 时，使用启动专属服务时给出的浏览器对照页，原有批次和定位仍在原审阅目录中。

## 启动故障与分发

| 现象 | 检查及恢复 |
| --- | --- |
| 找不到 Microsoft Word 或原件未打开 | 确认桌面 Word 可运行，再从 Word 的“文件 → 打开”选择同一完整路径。重新运行窗格服务，可省略 `--open-word`。未装桌面 Word 时，使用终端给出的浏览器专属地址。 |
| `Fixed port 43821 is already in use` 或 `port-reserved` | 检查原窗格终端和 `serve.py list --json`。仅在确认是自己启动的窗格后用 Ctrl+C 停止并切换。已分配给其他服务的端口应保留，期间可用浏览器审阅。 |
| Word 提示加载项无法访问 | 服务运行时，在 macOS 用 `/usr/bin/curl --fail https://127.0.0.1:43821/index.html`，在 Windows 用 `curl.exe --fail https://127.0.0.1:43821/index.html`。连接被拒绝时回看服务启动信息。TLS 报错时核对有效期、IP SAN 和系统对证书链的信任；macOS 可用 `security verify-cert -c "$CERT" -p ssl -s 127.0.0.1`。修复后回到 Word 重新加载并核对绑定。 |
| 窗格提示缺少 WordApi 1.6，或 Office.js 未加载 | 更新桌面 Word，或恢复对 `appsforoffice.microsoft.com` 的 HTTPS 访问并重新加载。macOS Word 实测用仅作用于窗格的策略阻止 Office.js 后，会显示恢复提示并禁用绑定。恢复正常页面后在 Word 重新加载，Office.js 就绪并成功绑定新的合成验收文档。此试验保留系统网络；完整断网、冷缓存及离线重开的结果待验。[Office.js 首次下载与缓存](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/referencing-the-javascript-api-for-office-library-from-its-cdn) |

上述侧载仅用于开发。组织内部分发候选是 Microsoft 365 管理中心的“集成应用”，需管理员在试点用户设备部署本机服务和证书，并实际验证清单、环回 URL 和功能区入口。公开上架还需满足各受支持平台和 Marketplace 认证要求。两条生产渠道均未验收，当前环回清单没有上架批准。[分发渠道](https://learn.microsoft.com/en-us/office/dev/add-ins/publish/publish) · [认证要求](https://learn.microsoft.com/en-us/legal/marketplace/certification-policies#1120-office-add-ins-word-excel-powerpoint-and-outlook)
