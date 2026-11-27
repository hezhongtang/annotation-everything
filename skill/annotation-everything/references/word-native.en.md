# Connect a review in Word

When the reviewer chooses desktop Word, confirm the full path of the saved local DOCX. The connection was exercised on Word for Mac 16.115.2 and requires WordApi 1.6. Give the reviewer the review’s browser URL as well. They can use it on other hosts, when Office.js is unavailable, or for signed or protected files.

## Start or reconnect

1. **Select the review.** For a new review, run `python3 "$S/build_viewer.py" --report "$DOCX" --out "$REVIEW" --name "$NAME" --locale en`. For an existing review, use `python3 "$S/serve.py" list --json` to match its name, ID and directory, then check the registered source path. Reuse its directory to retain batches and annotations. For files with the same name, compare both the directory and an identifier inside the document.
2. **Check the receiver.** Follow [Receiver binding and transfer](receiver-binding.en.md). Start or reuse `python3 "$S/serve.py" "$REVIEW" --no-open` and record its browser URL. An unbound legacy review can be opened for inspection. Sending work to a receiving session requires a binding. Explicitly transfer the binding before restarting the dedicated service for the new receiver.
3. **Start the pane service.** Use a trusted certificate with IP address 127.0.0.1 in its SAN. Keep its private key local. Run the command in a persistent terminal. Give the reviewer the new binding code directly from that terminal. Keep it out of review materials and submission records.

   ```sh
   python3 "$S/word_taskpane.py" "$REVIEW" --document "$DOCX" --cert "$CERT" --key "$KEY"
   ```

4. **Open Word.** On first use, copy the installed skill’s `$S/../word-taskpane/manifest.xml` into `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/` using Finder. Save work, quit Word completely and reopen the selected document. Under **Home → Add-ins → Developer Add-ins**, select **AE 本机审阅**. On later visits, select **打开审阅** on Home. To reload, choose **Reload** on the pane menu. Office.js requires access to Microsoft’s CDN on first load.
5. **Verify the connection.** Enter the code, select **绑定当前文档**, and check the displayed file path, review name and ID, current batch and receiver. Use **刷新连接状态** to see a new batch. Opening and reconnecting read the document. Applying a suggestion is a separate reviewer action.

## Post and manage native Word comments

Select the intended text directly in Word. If “五日” appears in both the body and a table, select it at the intended occurrence. Type in **批注或回复内容** and choose **发为 Word 批注**. Word can post comments with Track Changes off when the document permits comments. Private AE notes and point or box annotations stay in AE.

Choose **刷新 Word 讨论** to see the original comment, authors, replies and thread state. Choose **回复此线程**, **解决此线程** or **重开此线程** on the intended thread. Refresh after changing the thread in Word. Comments, tracked revisions and AE batch status have independent lifecycles. Once the pane confirms the change in the current document, save in Word. A recipient can continue the discussion directly in Word.

The service records each intent in `word-comments.json` inside the review directory before Word can act. On reconnection, expand **本次审阅的批注操作**. If an entry says **需核对**, check the selected text, authors and reply relationship in Word. After an undo, a moved anchor or a lost receipt, AE retains the record and prevents an uncertain replay. Keep this file with the review directory when moving the review.

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

Refresh the connection to see the original wording, suggestion, reason and source batch. Choose **在 Word 定位** to select the passage in Word. A reviewer can recall a pending batch or withdraw a returned suggestion. Changed paragraphs and repeated matches require verification. Select the intended text and submit a fresh batch.
