# Connect a review in Word

Use a saved local DOCX, desktop Microsoft Word with WordApi 1.6, and its running AE review. Development sideloading was verified on macOS 26.7.3 with Word 16.115.2. The Windows launcher and instructions are available for testing, with desktop verification pending. Give the reviewer the review’s browser URL as well, including for signed or protected files. In the commands below, set `$S` to the installed skill’s `scripts` directory.

## Enable the add-in once

- **Mac development sideload.** In Finder, use Go to Folder to open `~/Library/Containers/com.microsoft.Word/Data/Documents/`. Create `wef` there if needed. Copy the installed skill’s `word-taskpane/manifest.xml` into it. Finder can perform the copy when macOS denies container access to the terminal. Save work and quit Word completely. After opening the document below, choose **Home → Add-ins → Developer Add-ins → AE 本机审阅**. [Microsoft’s Mac sideloading instructions](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/sideload-an-office-add-in-on-mac)
- **Windows development sideload, awaiting desktop verification.** Place only `manifest.xml` in a test shared folder you can access. In Word, open **File → Options → Trust Center → Trust Center Settings → Trusted Add-in Catalogs**. Add the share’s UNC path and select **Show in Menu**. Save work and restart Word. Choose **Home → Add-ins → Advanced → Shared Folder → AE 本机审阅 → Add**. Keep the DOCX on a local drive. Ask the administrator to evaluate device policies if shares or loopback access are restricted. [Microsoft’s Windows sideloading instructions](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/create-a-network-shared-folder-catalog-for-task-pane-and-content-add-ins)
- **Certificate.** Supply a locally trusted server certificate whose SAN includes IP address `127.0.0.1`, and its matching PEM key. Keep the key on the device. The Mac test used a one-day self-signed leaf with SSL trust scoped to this IP in the current user’s login keychain. On managed devices, obtain a certificate and confirm trust with the administrator.

## Start or reconnect

1. **Select the review.** For a new review, run `python3 "$S/build_viewer.py" --report "$DOCX" --out "$REVIEW" --name "$NAME" --locale en`. For an existing review, use `python3 "$S/serve.py" list --json` to match its name, ID and directory, then check the registered source path. Reuse its directory to retain batches and annotations. For files with the same name, compare both the directory and an identifier inside the document.
2. **Check the receiver.** Follow [Receiver binding and transfer](receiver-binding.en.md). Start or reuse `python3 "$S/serve.py" "$REVIEW" --no-open` and record its browser URL. An unbound legacy review can be opened for inspection. Sending work to a receiving session requires a binding. Explicitly transfer the binding before restarting the dedicated service for the new receiver.
3. **Start the pane and open the original.** The `--open-word` option opens the registered file in Microsoft Word after checking the running review and document fingerprint. Run the command in a persistent terminal. Give the reviewer the new binding code directly from that terminal. Keep it out of review materials and submission records.

   ```sh
   python3 "$S/word_taskpane.py" "$REVIEW" --document "$DOCX" --cert "$CERT" --key "$KEY" --open-word
   ```

   In Windows PowerShell, set the same path variables and run `py -3 "$S/word_taskpane.py" "$REVIEW" --document "$DOCX" --cert "$CERT" --key "$KEY" --open-word`. Omit `--open-word` to open the original yourself in Word.

4. **Open the pane.** Choose **Home → 打开审阅**. Choose **Reload** from the pane menu when needed. Word can auto-open a designated pane when the document carries an auto-open setting, the add-in is already installed, and the distribution channel supports the feature. AE provides the ribbon button and leaves the setting out of delivery documents. Recipients can open the documents normally in Word. [Auto-open conditions](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/automatically-open-a-task-pane-with-a-document)
5. **Verify the connection.** Enter the code, select **绑定当前文档**, and check the displayed file path, review name and ID, current batch and receiver. Use **刷新连接状态** to see a new batch. Opening and reconnecting read the document. Applying a suggestion is a separate reviewer action.

## Post and manage native Word comments

Select the intended text directly in Word. If “五日” appears in both the body and a table, select it at the intended occurrence. Type in **批注或回复内容** and choose **发为 Word 批注**. Word can post comments with Track Changes off when the document permits comments. Private AE notes and point or box annotations stay in AE.

Choose **刷新 Word 讨论** to see the original comment, authors, replies and thread state. Choose **回复此线程**, **解决此线程** or **重开此线程** on the intended thread. Refresh after changing the thread in Word. Comments, tracked revisions and AE batch status have independent lifecycles. Once the pane confirms the change in the current document, save in Word. A recipient can continue the discussion directly in Word.

The service records each intent in `word-comments.json` inside the review directory before Word can act. If the selection changes before the pane calls Word, select the intended text again and retry. If the pane has already called Word and loses the receipt, it retains the record and blocks a duplicate. On reconnection, expand **本次审阅的批注操作**. If an entry says **需核对**, check the selected text, authors and reply relationship in Word. An undo or a moved anchor can also affect this check. Keep this file with the review directory when moving the review.

## After saving, switching files or stopping

- **Reopen the same original.** Connect with the code for that review. A normal Word save can change the package fingerprint. If the pane asks for verification, check the full path and an identifier in the document, stop the pane service, then explicitly run `python3 "$S/build_viewer.py" --rebuild "$REVIEW"`. Inspect changed or missing annotation locations in the rebuild report. With the dedicated review running, restart the pane service and use its new code. The rebuild retains the review ID and relocates annotations using paragraph identity and text digests.
- **Save As or switch to a file with the same name.** Select **重新选择审阅**. Return to the bound original or select a review with its own directory and the copy’s full source path. The pane origin serves one review at a time. To switch, stop your own pane service with Ctrl+C, start it with the other review and source path, and check its new code. Both dedicated browser services can remain open.
- **Review stopped, port changed hands or receiver transferred.** Start the dedicated service again for the intended review ID. Restart the pane service for that identity and enter its new code. If the service or Word API is unavailable or the file exceeds 16 MiB, open the browser URL from the dedicated service. Existing batches and paragraph annotations stay in their review directory.

## Submit a selection and receive suggestions

Select text in one or more consecutive paragraphs in Word. Choose **预览所选文字与讨论** to check original and current wording, changes and authors. Select the threads to include, then choose **将预览内容交给接收会话**. The pane displays the source batch number. Selected threads retain their quoted text, authors, available timestamps, replies and resolution state. Uncertain change attribution is marked for verification.

The bound receiver uses its existing `notes.py claim` or `notes.py wait` workflow. Use the selected passages in `wordSubmission.paragraphs` to prepare a UTF-8 JSON array. Copy the zero-based paragraph number `n`. The reason can quote a selected reply. Replacement and deletion require `from` to occur in the selected current text. Use an empty `to` for deletion. For an insertion, set `from` to an empty string and set `edge` to `Start` or `End` of the selected passage. An empty array completes a batch with no suggestions.

```json
[{"n":8,"from":"五日","to":"七日","reason":"Allow two additional days for the inspection requested in the reply."}]
```

```sh
python3 "$S/word_context.py" return "$REVIEW" --batch 1 --input suggestions.json --harness "$HARNESS" --session "$SESSION"
```

Refresh the connection to see the original wording, suggestion, reason and source batch. Choose **在 Word 定位** to select the passage in Word. A reviewer can recall a pending batch or withdraw a suggestion before its write is attempted. After saving the same original and explicitly rebuilding the review, you can continue with a suggestion if the paragraph ID is still unique, the paragraph text matches, and the submitted selection occurs once in that paragraph. If the pane requests a fresh submission, verify and reselect the original passage in Word.


## Write a suggestion as a Word revision

Use **Locate in Word** to inspect the source. To disambiguate a match, select the exact original words in the same paragraph or cell, then choose **Relocate to current selection**. For an insertion, select the submitted quotation. The suggestion specifies its start or end.

**Write as revision** checks document protection, tracking, the paragraph and the selection. Native revision writes require WordApiDesktop 1.4. When tracking is off, choose **Enable Word change tracking** first. Suggestions touching an existing comment, revision or its boundary stay pending for review in Word. Existing revisions elsewhere in the paragraph can remain independent.

The pane reports a write to the open Word document after reading the revised text, the recoverable original and the native author at that location. Use **Review → All Markup** to inspect line and page breaks, then save in Word. If the result needs checking, inspect the original page and read back the revisions. Word reports the save state.

Select one or more suggestions and choose **将所选建议写为修订**. The pane preflights the entire selection. Resolve each reported conflict by clearing its checkbox or relocating it. If someone edits a target during the batch, the pane stops and shows the remaining items as **未执行**.

The review directory keeps write intents and native readbacks in `word-revisions.json`. Reconnect to compare attempts with Word’s current revisions and authors. If the first receipt was lost, the pane also checks the Word author recorded before the write. An uncertain attribution remains marked **需核对**. Items confirmed in Word show the paragraph or table cell where they were written. Items marked **未执行** can be selected and checked again. Inspect items marked **需核对** in the document. Undo, acceptance and rejection can all change the native evidence. Word reports whether the document has been saved.

## Troubleshooting and distribution

| Symptom | Check and recovery |
| --- | --- |
| Microsoft Word cannot open the file | Check that desktop Word opens. Select the same full path under **File → Open** and restart the pane service without `--open-word`. If Word is unavailable, use the dedicated browser URL printed in the terminal. |
| `Fixed port 43821 is already in use` or `port-reserved` | Check the original pane terminal and `serve.py list --json`. Only stop your own pane service with Ctrl+C when switching. Keep any existing port reservations and use the browser review while the port is occupied. |
| Word cannot reach the add-in | While the service runs, try `/usr/bin/curl --fail https://127.0.0.1:43821/index.html` on Mac or `curl.exe --fail https://127.0.0.1:43821/index.html` on Windows. Check the service output for connection failures. For a TLS error, check expiration, IP SAN and system trust for the chain. On Mac, run `security verify-cert -c "$CERT" -p ssl -s 127.0.0.1`. Reload in Word after fixing the cause and check the binding. |
| WordApi 1.6 missing or Office.js unavailable | Update desktop Word or restore HTTPS access to `appsforoffice.microsoft.com`, then reload. In Mac Word, blocking Office.js for the pane displayed a recovery message and disabled binding. After restoring the normal pane and selecting Reload in Word, Office.js became ready and the reviewer bound a new synthetic document. The device stayed online. Cold-cache, disconnected device and offline reopen behavior still need verification. [Office.js download and cache behavior](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/referencing-the-javascript-api-for-office-library-from-its-cdn) |

Sideloading covers development. For internal distribution, an administrator can evaluate the Microsoft 365 **Integrated apps** portal, provision the local service and certificate on pilot devices, and verify that the manifest, loopback URL and ribbon button work under tenant policies. A public Marketplace listing also requires certification across supported platforms. Neither production route has been qualified for this loopback manifest. [Distribution channels](https://learn.microsoft.com/en-us/office/dev/add-ins/publish/publish) · [Certification requirements](https://learn.microsoft.com/en-us/legal/marketplace/certification-policies#1120-office-add-ins-word-excel-powerpoint-and-outlook)
