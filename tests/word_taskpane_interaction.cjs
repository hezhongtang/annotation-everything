// Exercise the shipped pane through its form and suggestion button. Word supplies range relations.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const page = fs.readFileSync(0, "utf8");
const scripts = Array.from(page.matchAll(/<script>([\s\S]*?)<\/script>/g), match => match[1]);
const suggestion = {id: "sample", from: "五日", to: "七日", paragraph: "应于五日内回复", bodyParagraph: 0};

// Synthetic Office boundary, including the Flat OPC DOM exposed by the webview.
class DOMParser {
  parseFromString(json) {
    const w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const pkg = "http://schemas.microsoft.com/office/2006/xmlPackage";
    function node(localName, children = [], attrs = {}, namespaceURI = w) {
      const result = {localName, namespaceURI, textContent: "", getAttributeNS: (_ns, key) => attrs[key],
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
    const runs = JSON.parse(json).map(({type, text, author}) => {
      const value = node(type === "Deleted" ? "delText" : "t");
      value.textContent = text;
      const run = node("r", [value]);
      return !type ? run : node(type === "Deleted" ? "del" : "ins", [run], {author, date: "2026-11-27"});
    });
    return node("package", [node("part", [node("body", [node("p", runs)])], {name: "/word/document.xml"}, pkg)], {}, pkg);
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
    tag, dataset: {}, children: [], events: {}, value: "",
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
    createElement: element
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

async function applyInWord({ fileChanged = false, direct = false, relations = [], unreadable = false, table = false, readOnly = false, replacement = {}, old = false, missingAuthor = false, loss = false, tracking = "TrackAll", protectedDoc = false, enable = false, oldRelation = "After", manual = false, wrongSelection = false }) {
  const item = {...suggestion, paragraph: (old ? "邮件" : "") + suggestion.paragraph, ...(table && {table: 0, row: 0, column: 1}), ...replacement};
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
  const paragraph = {
    parentContentControlOrNullObject: outsideControl, parentTableCellOrNullObject: outsideControl,
    getRange: () => ({
      ...plainRange(projection(segments(), "Original"), projection(segments(), "Current")),
      getOoxml: () => ({value: JSON.stringify(segments())}),
      getRange: () => ({expandTo: () => plainRange("", projection(prefix, "Current"))}),
      search: () => collection([target])
    }),
    getTrackedChanges: () => collection(segments().filter(r => r.type).map(r => ({
      ...r, getRange: () => ({...plainRange(r.type === "Deleted" ? r.text : "", r.type === "Added" ? r.text : ""), relation: oldRelation})
    })))
  };
  const comments = relations.map(relation => ({ getRange() {
    if (unreadable) throw new Error("Office range unavailable");
    return {relation};
  } }));
  let file = "/synthetic/A/contract.docx";
  const context = {sync: async () => {if (loss && writes.length) throw new Error("Word 未应答");}, document: {
    getSelection: () => wrongSelection ? {} : selected,
    changeTrackingMode: tracking, isReadOnly: readOnly, protectionType: protectedDoc ? "AllowOnlyReading" : "NoProtection", load() {},
    body: {
      tables: collection([{load() {}, isUniform: true, nestingLevel: 1, parentContentControlOrNullObject: outsideControl, getCell: () => ({body: {paragraphs: collection([paragraph])}})}]),
      paragraphs: collection([paragraph]), getRange: () => ({getComments: () => collection(comments)})
    }
  }};
  const Word = {run: action => action(context)};
  const service = revisionService([item]);
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

(async () => {
  if (process.argv[2]) return batchScenario(process.argv[2], openPane);
  const protectedResult = await applyInWord({readOnly: true});
  assert.deepEqual(protectedResult.writes, [], "read-only Word must retain the suggestion");
  for (const table of [true, false]) {
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
