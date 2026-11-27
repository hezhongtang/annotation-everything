// Exercise the shipped pane through its form and suggestion button. Word supplies range relations.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const page = fs.readFileSync(0, "utf8");
const scripts = Array.from(page.matchAll(/<script>([\s\S]*?)<\/script>/g), match => match[1]);
const suggestion = {id: "sample", from: "五日", to: "七日", paragraph: "应于五日内回复", bodyParagraph: 0, paraId: "1A000001"};

// Synthetic Office boundary, including the Flat OPC DOM exposed by the webview.
class DOMParser {
  parseFromString(json) {
    const w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const pkg = "http://schemas.microsoft.com/office/2006/xmlPackage";
    function node(localName, children = [], attrs = {}, namespaceURI = w) {
      const result = {localName, namespaceURI, children, textContent: "", getAttributeNS: (_ns, key) => attrs[key],
        getElementsByTagNameNS(ns, name) {
          return children.flatMap(c => [
            ...((ns === "*" || ns === c.namespaceURI) && (name === "*" || name === c.localName) ? [c] : []),
            ...c.getElementsByTagNameNS(ns, name)
          ]);
        }
      };
      children.forEach(c => { c.parentElement = result; });
      return result;
    }
    const pkgPart = (name, blocks) => node("part", [node("document", [node("body", blocks)])], {name}, pkg);
    const {runs: segments, paraId, layout} = JSON.parse(json);
    if (layout) {
      const paragraph = spec => {
        const {id, text} = spec && typeof spec === "object" ? spec : {id: spec, text: ""};
        const t = node("t");
        t.textContent = text;
        return node("p", text ? [node("r", [t])] : [], {paraId: id});
      };
      const blocks = layout.map(block => Array.isArray(block) ? node("tbl", block.map(row =>
        node("tr", row.map(id => node("tc", [paragraph(id)]))))) : paragraph(block));
      return node("package", [pkgPart("/word/header1.xml", [paragraph("1A000001")]), pkgPart("/word/document.xml", blocks)], {}, pkg);
    }
    const runs = segments.map(({type, text, author}) => {
      const value = node(type === "Deleted" ? "delText" : "t");
      value.textContent = text;
      const run = node("r", [value]);
      return !type ? run : node(type === "Deleted" ? "del" : "ins", [run], {author, date: "2026-11-27"});
    });
    return node("package", [pkgPart("/word/document.xml", [node("p", runs, {paraId})])], {}, pkg);
  }
}

function matches(node, selector) {
  const tag = selector.match(/^\w+/)?.[0];
  return (!tag || node.tag === tag) && (!selector.includes(":checked") || node.checked) &&
    (!selector.includes("[data-id]") || node.dataset.id) &&
    (!selector.includes("[type=checkbox]") || node.type === "checkbox");
}

function element(tag) {
  return {
    tag, dataset: {}, style: {}, children: [], events: {}, value: "",
    addEventListener(name, listener) { this.events[name] = listener; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    removeAttribute(name) { delete this[name]; },
    querySelectorAll(name) {
      return this.children.filter(node => typeof node !== "string")
        .flatMap(node => [ ...(matches(node, name) ? [node] : []), ...node.querySelectorAll(name) ]);
    }
  };
}

const {revisionService, batchScenario} = require("./fixtures/word_revision_batch.cjs");

async function openPane(Word, fetch, files = {url: "/synthetic/A/contract.docx"}) {
  const nodes = Object.fromEntries(
    Array.from(page.matchAll(/<(\w+)\b[^>]*?\bid="([^"]+)"/g), ([, tag, id]) => [id, element(tag)])
  );
  const document = {
    getElementById: id => nodes[id] || null,
    addEventListener() {},
    createElement: element, createTextNode: value => value
  };
  const office = {
    HostType: { Word: "Word" }, PlatformType: { Mac: "Mac", PC: "PC" }, AsyncResultStatus: { Succeeded: "ok" },
    onReady: callback => callback({host: "Word", platform: "Mac"}),
    context: {
      platform: "Mac",
      requirements: {isSetSupported: () => true}, diagnostics: {},
      document: {getFilePropertiesAsync: callback => callback({status: "ok", value: {url: files.url}})}
    }
  };
  const pane = vm.createContext({
    DOMParser, Office: office, window: {Office: office, addEventListener() {}}, Word, document, fetch,
    crypto: webcrypto, TextEncoder, setTimeout, clearTimeout
  });
  for (const script of scripts) vm.runInContext(script, pane);
  nodes.token.value = "synthetic";
  await nodes.connect.events.submit({preventDefault() {}});
  return nodes;
}

async function applyInWord({ incompleteCell = false, outsideOnly = false, fileChanged = false, direct = false, relations = [], unreadable = false, table = false, readOnly = false, replacement = {}, old = false, missingAuthor = false, loss = false, tracking = "TrackAll", protectedDoc = false, enable = false, oldRelation = "After", manual = false, wrongSelection = false, liveParaId = suggestion.paraId, duplicateId = false, extraXmlParagraph = false, exportTail }) {
  const item = {...suggestion, paragraph: (old ? "邮件" : "") + suggestion.paragraph, ...(table && {table: 0, row: 0, column: 1}), ...replacement};
  if (table) delete item.bodyParagraph;
  const writes = [];
  const collection = items => ({items, load() {}});
  const prefix = [{text: "应于"}];
  if (old) prefix.unshift({type: "Deleted", text: "传真", author: "甲"}, {type: "Added", text: "邮件", author: "甲"});
  const suffix = [{text: "内回复"}];
  const segments = () => {
    const native = (type, text) => ({type, text, author: missingAuthor ? "" : "乙"});
    if (!writes.length) return [...prefix, {text: "五日"}, ...suffix];
    if (!item.from) return [...prefix, ...(item.edge === "Start" ? [native("Added", item.to), {text: "五日"}] :
      [{text: "五日"}, native("Added", item.to)]), ...suffix];
    return [...prefix, native("Deleted", "五日"), ...(item.to ? [native("Added", item.to)] : []), ...suffix];
  };
  const projection = (runs, state) => runs.filter(r => r.type !== (state === "Original" ? "Added" : "Deleted")).map(r => r.text).join("");
  const plainRange = (original, current) => ({
    load() {return this;}, text: current, getReviewedText: state => ({value: state === "Original" ? original : current})
  });
  let selected;
  const target = {
    ...plainRange("五日", "五日"), getRange(edge) { return edge === "Start" ? targetStart : targetEnd; },
    getComments: () => collection(direct ? [{}] : []),
    compareLocationWith(range) { return {value: this === range ? "Equal" : range.relation}; },
    select() { selected = this; }, delete() {writes.push(["", "Delete"]);},
    insertText(text, location) { writes.push([text, location]); }
  };
  const targetStart = {...target, text: ""}, targetEnd = {...target, text: ""};
  const outsideControl = {isNullObject: true, load() {}};
  function nativeChange(r, location) {
    return {...r, getRange: () => ({
      ...plainRange(r.type === "Deleted" ? r.text : "", r.type === "Added" ? r.text : ""),
      relation: oldRelation, compareLocationWith: () => ({value: location})
    })};
  }
  const start = {compareLocationWith: other => ({value: other === start ? "Equal" : "Before"})};
  const paragraph = {
    parentContentControlOrNullObject: outsideControl, parentTableCellOrNullObject: outsideControl,
    getRange: edge => edge === "Start" ? start : ({
      ...plainRange(projection(segments(), "Original"), projection(segments(), "Current")),
      getOoxml: () => ({value: JSON.stringify({runs: segments(), paraId: "53B2FA15"})}),
      getRange: () => ({expandTo: () => plainRange("", projection(prefix, "Current"))}),
      search: () => collection([target])
    }),
    getTrackedChanges: () => collection(segments().filter(r => r.type && !(incompleteCell && r.type === "Added"))
      .map(r => nativeChange(r, "Inside")))

  };
  const comments = relations.map(relation => ({ getRange() {
    if (unreadable) throw new Error("Office range unavailable");
    return {relation};
  } }));
  let file = "/synthetic/A/contract.docx";
  const tailId = "1A0000AB";
  const xmlTail = exportTail && {id: exportTail.xmlId || tailId, text: exportTail.xmlText ?? "真实末段"};
  const liveTail = text => ({getRange: () => plainRange(text, text)});
  const context = {sync: async () => {if (loss && writes.length) throw new Error("Word 未应答");}, document: {
    getSelection: () => wrongSelection ? {} : selected,
    changeTrackingMode: tracking, isReadOnly: readOnly, protectionType: protectedDoc ? "AllowOnlyReading" : "NoProtection", load() {},
    body: {
      getOoxml: () => ({value: JSON.stringify({layout: [
        ...(table ? [[["1B000010", liveParaId]]] : [liveParaId]), ...(duplicateId || extraXmlParagraph ? [liveParaId] : []),
        ...(exportTail ? (exportTail.middle ? [null, xmlTail] : [xmlTail, null]) : [])
      ]})}),
      getTrackedChanges: () => collection(segments().filter(r => r.type).map(r =>
        nativeChange(r, outsideOnly && r.type === "Added" ? "Before" : "InsideStart"))
        .concat(nativeChange({type: "Added", text: "七日", author: "乙"}, "After"))),
      tables: collection(table ? [{load() {}, isUniform: true, nestingLevel: 1, parentContentControlOrNullObject: outsideControl, getCell: () => ({body: {paragraphs: collection([paragraph])}})}] : []),
      paragraphs: collection([...(table ? [liveTail("左栏"), paragraph] : [paragraph]), ...(duplicateId ? [liveTail("副本")] : []),
        ...(exportTail ? [liveTail(exportTail.hostText ?? "真实末段")] : [])]),
      getRange: () => ({getComments: () => collection(comments)})
    }
  }};
  const Word = {run: action => action(context)};
  const service = revisionService([item], {sourceTailId: tailId});
  const nodes = await openPane(Word, service.fetch, {get url() { return file; }});
  if (fileChanged) file = "/synthetic/B/contract.docx";
  const writeButton = () => nodes.suggestions.querySelectorAll("button").find(button => button.dataset.write === "true");
  if (enable) {
    await nodes["enable-tracking"].events.click();
    assert.deepEqual(writes, [], "enabling tracking never writes a suggestion");
  }
  if (manual) {
    selected = target;
    const relocate = nodes.suggestions.querySelectorAll("button").find(button => button.textContent === "用当前选区重新定位");
    await relocate.events.click();
    assert.deepEqual(writes, [], "reselecting the target never writes a suggestion");
  }
  const button = writeButton();
  assert.ok(button, nodes.status.textContent);
  await button.events.click();
  return {writes, status: nodes.status.textContent, disabled: writeButton()?.disabled};
}

async function resubmitAfterReconnect() {
  const collection = items => ({items, load() {}});
  const empty = {isNullObject: true, load() {}};
  let bodyReads = 0;
  function paragraph(text, comments = []) {
    const start = {compareLocationWith: other => ({value: other === start ? "Equal" : "Before"})};
    const range = {text, load() {return this;}, getReviewedText: () => ({value: text}),
      getOoxml: () => ({value: JSON.stringify({runs: [{text}], paraId: "53B2FA15"})}),
      intersectWith() {return this;}, getTrackedChanges: () => collection([]), getComments: () => collection(comments)};
    return {getRange: edge => edge === "Start" ? start : range,
      parentTableCellOrNullObject: empty, parentContentControlOrNullObject: empty};
  }
  const comment = {id: "T", content: "未勾选的讨论", authorName: "甲", replies: collection([]),
    getRange: () => ({text: "日", load() {return this;}})};
  const first = paragraph("五日", [comment]);
  const outside = paragraph("仅在本机的未选文字");
  const cell = paragraph("五日");
  const tail = paragraph("源文件末段");
  const table = {load() {}, isUniform: true, nestingLevel: 1, parentContentControlOrNullObject: empty,
    getCell: () => ({body: {paragraphs: collection([cell])}})};
  const context = {sync: async () => {}, document: {
    getSelection: () => ({paragraphs: collection([first, cell])}),
    body: {paragraphs: collection([first, outside, cell, tail]), tables: collection([table]),
      getOoxml() {
        bodyReads++;
        return {value: JSON.stringify({layout: ["1A000006", "1A000003", [["1A000001"]], {id: "1A000007", text: "源文件末段"}, null]})};
      }}
  }};
  const Word = {run: action => action(context)};
  const receiver = {state: "waiting", bindingId: "receiver-A", sessionId: "s", harness: "Codex"};
  const attempts = [];
  const batches = new Map(), recalled = new Set(), acknowledged = [];
  let lose = true;
  const other = revisionService([]);
  const fetch = async (path, request) => {
    const reply = data => ({ok: true, json: async () => data});
    if (path === "/api/bind" || path === "/api/status") return reply({reviewId: "R", sourceTailId: "1A000007", receiver, suggestions: []});
    if (path === "/api/word/submit") {
      const attempt = JSON.parse(request.body);
      attempts.push(attempt);
      if (!batches.has(attempt.submitId)) batches.set(attempt.submitId, 5 + batches.size);
      const batch = batches.get(attempt.submitId);
      if (recalled.has(batch)) return {ok: false, status: 409, json: async () => ({error: "word-batch-recalled"})};
      if (lose) { lose = false; throw Error("Synthetic response lost after commit"); }
      return reply({batch});
    }
    if (path === "/api/word/ack") {
      acknowledged.push(JSON.parse(request.body).batch);
      return reply({});
    }
    if (path === "/api/word/list") {
      return reply({suggestions: [], revisionOperations: [], pendingBatches: [...batches.values()].filter(n => !recalled.has(n))});
    }
    if (path === "/api/word/recall") {
      const {batch} = JSON.parse(request.body);
      recalled.add(batch);
      return reply({recalledSubmitIds: [...batches].filter(([, n]) => n === batch).map(([id]) => id)});
    }
    return other.fetch(path, request);
  };
  const nodes = await openPane(Word, fetch);
  const reconnect = async () => {
    nodes.token.value = "synthetic";
    await nodes.connect.events.submit({preventDefault() {}});
  };
  const submit = async () => {
    await nodes["word-preview"].events.click();
    await nodes["word-submit"].events.click();
  };
  await submit();
  assert.match(nodes.status.textContent, /已断开/);
  await reconnect();
  await submit();
  await submit();
  assert.equal(attempts.length, 3);
  assert.deepEqual(attempts[0], attempts[1], "Rebinding and recreating the preview retry the same approved selection.");
  assert.notEqual(attempts[2].submitId, attempts[0].submitId, "A new explicit submission after receipt has its own ID.");
  assert.equal(bodyReads, 3, "One full body snapshot serves all selected paragraphs in each preview.");

  const lastId = () => attempts[attempts.length - 1].submitId;
  for (const localRecall of [true, false]) {
    lose = true;
    await submit();
    const lostId = lastId(), batch = batches.get(lostId);
    assert.match(nodes.status.textContent, /已断开/);
    if (!localRecall) recalled.add(batch);   // Another pane received the recall receipt.
    await reconnect();
    if (localRecall) {
      const button = nodes["word-pending"].children.find(b => b.textContent === "收回待接收的第 " + batch + " 批");
      assert.ok(button);
      await button.events.click();
      assert.match(nodes.status.textContent, /已收回/);
    } else {
      await submit();
      assert.equal(lastId(), lostId);
      assert.match(nodes.status.textContent, /已收回/);
      assert.doesNotMatch(nodes.status.textContent, /已提交/);
    }
    assert.ok(!acknowledged.includes(batch));
    await submit();
    assert.notEqual(lastId(), lostId, "After recall, previewing again submits a fresh batch.");
    assert.equal(nodes.status.textContent, "已提交第 " + batches.get(lastId()) + " 批。");
  }
  for (const attempt of attempts) {
    assert.deepEqual(attempt.paragraphs.map(p => [p.n, p.paraId, p.current, p.threads]), [
      [0, "1A000006", "五日", []], [2, "1A000001", "五日", []]
    ]);
    assert.ok(!JSON.stringify(attempt).includes("仅在本机"));
  }
}

(async () => {
  if (process.argv[2] === "resubmit") return resubmitAfterReconnect();
  if (process.argv[2]) return batchScenario(process.argv[2], openPane);
  const recoveredCell = await applyInWord({table: true, incompleteCell: true});
  assert.match(recoveredCell.status, /已写入当前 Word 文档/);
  const unrelatedEvidence = await applyInWord({table: true, incompleteCell: true, outsideOnly: true});
  assert.match(unrelatedEvidence.status, /需核对/);
  const protectedResult = await applyInWord({readOnly: true});
  assert.deepEqual(protectedResult.writes, [], "read-only Word must retain the suggestion");
  for (const table of [true, false]) {
    assert.match((await applyInWord({table, exportTail: {}})).status, /已写入当前 Word 文档/);
    for (const exportTail of [{middle: true}, {xmlText: "", hostText: ""}, {xmlId: "1A0000CC"}, {hostText: "已改变"}]) {
      const result = await applyInWord({table, exportTail});
      assert.deepEqual(result.writes, [], "An uncorroborated extra paragraph must never be discarded.");
      assert.match(result.status, /段落/);
    }
    for (const structure of [{duplicateId: true}, {extraXmlParagraph: true}]) {
      const result = await applyInWord({table, ...structure});
      assert.deepEqual(result.writes, [], "An ambiguous ID or differing Word paragraph count must stop the write.");
      assert.match(result.status, /段落/);
    }
    for (const liveParaId of ["1A00000F", null]) {
      const moved = await applyInWord({table, liveParaId, manual: true});
      assert.deepEqual(moved.writes, [], "An identical paragraph at the former index must never receive the edit.");
      assert.match(moved.status, /段落标识/);
    }
    for (const [replacement, writes] of [
      [{from: "", selection: "五日", edge: "Start", to: "约"}, [["约", "Start"]]],
      [{to: ""}, [["", "Delete"]]]
    ]) {
      const result = await applyInWord({table, replacement});
      assert.deepEqual(result.writes, writes, result.status);
      assert.match(result.status, /已写入当前 Word 文档/);
    }
  }
  assert.match((await applyInWord({old:true})).status, /已写入当前 Word 文档/);
  for (const options of [{missingAuthor:true}, {loss:true}]) {
    const result = await applyInWord(options);
    assert.match(result.status, /需核对/);
    assert.equal(result.disabled, true);
  }
  for (const options of [{tracking:"Off"}, {protectedDoc:true}]) {
    const result = await applyInWord(options);
    assert.deepEqual(result.writes, []);
    assert.equal(result.disabled, false);
  }
  assert.match((await applyInWord({tracking: "Off", enable: true})).status, /已写入当前 Word 文档/);
  assert.match((await applyInWord({manual: true, replacement: {status: "needs-relocation"}})).status, /已写入当前 Word 文档/);
  for (const options of [{old:true, oldRelation:"Equal"}, {wrongSelection:true}, {replacement: {status:"needs-relocation"}}]) {
    const result = await applyInWord(options);
    assert.deepEqual(result.writes, [], result.status);
  }
  const refused = [
    {table: true, direct: true}, {relations: ["Equal"]}, {relations: ["InsideStart"]}, {relations: ["OverlapsBefore"]},
    {relations: ["AdjacentBefore"]}, {relations: ["AdjacentAfter"]}, {relations: ["unknown"]},
    {relations: ["Before"], unreadable: true}, {fileChanged: true}
  ];
  for (const conditions of refused) {
    const result = await applyInWord(conditions);
    assert.deepEqual(result.writes, [], JSON.stringify(conditions));
    if (!conditions.fileChanged) assert.equal(result.disabled, false, "blocked suggestion must remain pending");
    const expected = conditions.fileChanged ? /当前文档与绑定时不同/ : /未写入修订，建议已保留/;
    assert.match(result.status, expected, JSON.stringify(conditions));
  }
  for (const options of [{table: true, relations: ["Before"]}, {relations: ["Before", "After", "Unrelated"]}, {}]) {
    const result = await applyInWord(options);
    assert.deepEqual(result.writes, [["七日", "Replace"]]);
    assert.equal(result.disabled, true);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
