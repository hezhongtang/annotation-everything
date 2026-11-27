# 在 macOS Word 试用本机任务窗格

用这个原型可以在两份正在运行的 AE 审阅中绑定指定文档，在 Word 打印布局中查看和处理跟踪修订。使用本地保存的合成 DOCX 和支持 WordApi 1.6 的 macOS 桌面 Word。建议和 Word API 的回读结果只显示在任务窗格中。

本次宿主版本、观测结果与待验项见[macOS 实机记录](word-taskpane-macos.md)。

## 准备两份审阅

从仓库根目录运行，在新目录中保留此轮的合同、审阅和独立台账。下面几条命令使用同一个终端。

```sh
umask 077
DEMO=$(mktemp -d)
DEMO=$(cd "$DEMO" && pwd -P)
export AE_HOME="$DEMO/ledger"
S=skill/annotation-everything/scripts
python3 fixtures/docx/make_word_native.py --prepare-reviews "$DEMO"
python3 "$S/serve.py" "$DEMO/A/review" --no-open > "$DEMO/a.log" 2>&1 &
python3 "$S/serve.py" "$DEMO/B/review" --no-open > "$DEMO/b.log" 2>&1 &
python3 "$S/serve.py" list --json
```

等两个审阅服务都显示各自的端口。A、B 的文件都叫 `contract.docx`，正文编号分别是 `AE-214-A` 和 `AE-214-B`。两份审阅都有表格和页尾两条替换建议。

## HTTPS 与清单

准备用户钥匙串或受管理设备信任的本地服务证书和对应 PEM 私钥，证书的 SAN 必须包含 **IP 地址 127.0.0.1**。已有组织签发的受信证书可直接使用。在自有测试机上，可依照本轮实测方式生成一天有效的自签名服务证书，只为当前用户、SSL 用途和该环回 IP 添加信任。

```sh
cat > "$DEMO/loopback.cnf" <<'CERT_CONFIG'
[req]
prompt = no
distinguished_name = name
x509_extensions = server
[name]
CN = AE Word local prototype
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
CERT_CONFIG
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -config "$DEMO/loopback.cnf" \
  -out "$DEMO/loopback.pem" -keyout "$DEMO/loopback-key.pem"
security add-trusted-cert -r trustRoot -p ssl -s 127.0.0.1 \
  -k "$HOME/Library/Keychains/login.keychain-db" "$DEMO/loopback.pem"
security verify-cert -c "$DEMO/loopback.pem" -p ssl -s 127.0.0.1
python3 "$S/word_taskpane.py" "$DEMO/A/review" --document "$DEMO/A/contract.docx" \
  --cert "$DEMO/loopback.pem" --key "$DEMO/loopback-key.pem"
```

终端输出固定 URL、审阅标识与本次启动的绑定码。让服务保持前台运行。在另一个终端中可以用 `curl --fail https://127.0.0.1:43821/index.html` 确认该客户端的 HTTPS 信任；Word WebKit 的信任以窗格实际加载为准。固定端口被占用时原型启动失败。释放自己启动的占用进程后重试，核对新绑定码。

把仓库内的 XML 清单放入 Word 的测试清单目录。首次侧载后启动 Word；已有 Word 会话请保存文档，再退出并重开。在“开始 → 加载项”中选中“AE 本机审阅”，在“开始”的“AE 本机审阅”组按“打开审阅”。[Microsoft 的 Mac 侧载步骤](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/sideload-an-office-add-in-on-mac)

```sh
WEF="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef"
mkdir -p "$WEF"
cp skill/annotation-everything/word-taskpane/manifest.xml "$WEF/ae-word-taskpane-prototype.xml"
```

## 连接与修订

1. 在 Word 中打开 A 的 `contract.docx`，选择打印布局。在窗格输入终端的绑定码，点击“绑定当前文档”，核对审阅标识。用同一绑定码打开 B 时，窗格应拒绝绑定。B 的 AE 专属端口可继续独立使用。
2. 在 Word “审阅”中打开修订，显示选择“所有标记”。点击第一条“应用这条修订”。表格第一行第二列应出现“五日”删除、“七日”插入。正文“收件方式……”段落中的“五日”应保持原样。记录可见修订、作者和原评论线程。
3. 在保存前点第二条建议，扩写靠近第一页底部的“本合同自签收即生效。”。查看换行与分页，记录改前和改后的段落位置、页码以及当前标记视图。字体和打印设置会影响分页，验收采用 Word 实际版面。
4. 在 Word 中分别选中“验收范围包括样机及备用件。”所在段及以“分别登记”起首的段，点击“读取选区与正文修订”。记录宿主版本、选区可见文字、Original / Current、修订范围文本和作者。第一段的预置删除为“及备用件”。第二段预置甲插入“共同”，紧接着删除“分别”。回读缺字或冲突时保留原值，以“所有标记”、两份副本接受与拒绝结果、保存后 OOXML 互证。
5. 保存并关闭文档。从保存后的 A 创建两份副本，在 Word 中分别**只接受、只拒绝本次新增的表格替换**。新副本应显示“七日”，旧副本应显示“五日”。观察预置作者甲的旧修订，以及乙的批注与甲的回复是否能独立处理。回读和截图均使用合成数据。
6. 在首次联网加载与断网重开两种条件下分别记录窗格是否显示、Office.js 是否就绪、能否绑定和应用。区分首次加载与命中 WebKit 缓存。页面从 Office 官方 CDN 请求 Office.js；首次加载需要能访问该来源。退出时用 Ctrl+C 关闭原型，终止本轮绑定。随后在原终端执行下列命令停止专属服务。

```sh
python3 "$S/serve.py" stop "$DEMO/A/review"
python3 "$S/serve.py" stop "$DEMO/B/review"
```

保存、另存、重新启动审阅服务或切换到另一份合同后，须重新核对来源。原型每次提供建议前核对构建时的文件指纹。要重复演示，可在新的临时目录重做准备步骤。测试结束后可移除本次复制的清单。用 `security remove-trusted-cert "$DEMO/loopback.pem"` 移除本轮演示证书的信任。

## 接口和分发范围

- 服务只监听 `127.0.0.1:43821`。读取白名单为 `/index.html` 与清单的三张 `/api/icon/*.png`。服务以 CLI 指定的审阅和源文件为界，按台账记录的专属端口核对 `/api/health`，并在本机调用已有的批注读取、段落定位及修订限制逻辑。
- `POST /api/bind` 接受 `documentKey`，返回审阅名与标识、文件名、当前批次、接收方状态、该审阅的浏览器地址与可用建议。`POST /api/status` 接受 `documentKey` 和 `reviewId`，刷新当前连接状态。`POST /api/suggestion` 另要求批注 `id`，点击时重新核对建议。三个接口均校验固定 Host、同源 Origin、JSON 请求体和本次启动的 Bearer 绑定码。文档位置来自 Word 文件属性，本地绝对路径按 NFC 规范化后以 SHA-256 摘要传输。在 macOS 上仅接受经解析确认为同一文件的系统目录别名（`/tmp`、`/var`）。审阅服务实例、接收方绑定或源文件指纹改变后，旧绑定失效。恢复入口见[在 Word 中连接审阅](../skill/annotation-everything/references/word-native.zh-CN.md)。
- 当前目标范围为顶层正文段落和普通表格中仅含一个段落的单元格，目标原词必须唯一。原型支持最大 16 MiB 的合成 DOCX。编辑动作由 Word 的 Range 生成原生修订。旧修订、跨段和不确定位置会触发限制，AE 批注保留待处理状态。固定来源一次服务一份已启动的审阅。要绑定 B，退出 A 的原型后用 B 的目录和文档重新启动。
- 本机侧载用于开发验证。组织部署的候选渠道为 Microsoft 365 管理中心的“集成应用”，每台设备仍需部署本机服务和证书信任。面向公众的 Marketplace 有独立的跨平台验证与审核要求。本原型没有组织部署或商店通过记录。若设备策略阻止环回 HTTPS 或用户信任，下一次试验可由组织签发和下发本机证书，继续使用同一环回来源与绑定机制。[Microsoft 的分发渠道说明](https://learn.microsoft.com/en-us/office/dev/add-ins/publish/publish)
