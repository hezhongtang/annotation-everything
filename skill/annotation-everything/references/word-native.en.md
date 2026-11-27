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

## After saving, switching files or stopping

- **Reopen the same original.** Connect with the code for that review. A normal Word save can change the package fingerprint. If the pane asks for verification, check the full path and an identifier in the document, stop the pane service, then explicitly run `python3 "$S/build_viewer.py" --rebuild "$REVIEW"`. Inspect changed or missing annotation locations in the rebuild report. With the dedicated review running, restart the pane service and use its new code. The rebuild retains the review ID and relocates annotations using paragraph identity and text digests.
- **Save As or switch to a file with the same name.** Select **重新选择审阅**. Return to the bound original or select a review with its own directory and the copy’s full source path. The pane origin serves one review at a time. To switch, stop your own pane service with Ctrl+C, start it with the other review and source path, and check its new code. Both dedicated browser services can remain open.
- **Review stopped, port changed hands or receiver transferred.** Start the dedicated service again for the intended review ID. Restart the pane service for that identity and enter its new code. If the service or Word API is unavailable or the file exceeds 16 MiB, open the browser URL from the dedicated service. Existing batches and paragraph annotations stay in their review directory.
