// Exercise the shipped pane through its form and suggestion button. Word supplies range relations.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const page = fs.readFileSync(0, "utf8");
const scripts = Array.from(page.matchAll(/<script>([\s\S]*?)<\/script>/g), match => match[1]);
const suggestion = {id: "sample", from: "五日", to: "七日", paragraph: "应于五日内回复", bodyParagraph: 0};

function element(tag) {
  return {
    tag, dataset: {}, children: [], events: {}, value: "",
    addEventListener(name, listener) { this.events[name] = listener; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    removeAttribute(name) { delete this[name]; },
    querySelectorAll(name) {
      return this.children.flatMap(node => [ ...(node.tag === name ? [node] : []), ...node.querySelectorAll(name) ]);
    }
  };
}

async function applyInWord({ fileChanged = false, direct = false, relations = [], unreadable = false, table = false }) {
  const item = table ? {...suggestion, table: 0, row: 0, column: 1} : suggestion;
  const nodes = {};
  const document = {
    getElementById: id => nodes[id] ||= element(id),
    addEventListener() {},
    createElement: element
  };
  const office = {
    HostType: { Word: "Word" }, PlatformType: { Mac: "Mac" }, AsyncResultStatus: { Succeeded: "ok" },
    onReady: callback => callback({host: "Word", platform: "Mac"}),
    context: {
      requirements: {isSetSupported: () => true}, diagnostics: {},
      document: {getFilePropertiesAsync: callback => callback({status: "ok", value: {url: file}})}
    }
  };
  const writes = [];
  const collection = items => ({items, load() {}});
  const target = {
    text: suggestion.from,
    getComments: () => collection(direct ? [{}] : []),
    compareLocationWith: range => ({value: range.relation}),
    insertText(text, location) { writes.push([text, location]); return { select() {} }; }
  };
  const original = suggestion.paragraph;
  const current = () => writes.length ? "应于七日内回复" : original;
  const paragraph = {
    getRange: () => ({
      load() {}, get text() { return current(); },
      getReviewedText: state => ({value: state === "Original" ? original : current()}),
      search: () => collection([target])
    }),
    getTrackedChanges: () => collection(writes.length ? [{type: "Added"}, {type: "Deleted"}] : [])
  };
  const comments = relations.map(relation => ({ getRange() {
    if (unreadable) throw new Error("Office range unavailable");
    return {relation};
  } }));
  let file = "/synthetic/A/contract.docx";
  const context = {sync: async () => {}, document: {
    changeTrackingMode: "TrackAll", load() {},
    body: {
      tables: collection([{getCell: () => ({body: {paragraphs: collection([paragraph])}})}]),
      paragraphs: collection([paragraph]), getRange: () => ({getComments: () => collection(comments)})
    }
  }};
  const Word = {run: action => action(context)};
  const state = {reviewId: "A", title: "合成 A", documentName: "contract.docx", receiver: {state: "unconfigured"}};
  const responses = {
    "/api/bind": {...state, suggestions: [item]},
    "/api/status": state,
    "/api/suggestion": {reviewId: "A", suggestion: item},
    "/api/word/list": {suggestions: [item], pendingBatches: []}
  };
  const fetch = async path => {
    assert.ok(Object.hasOwn(responses, path), path);
    return {ok: true, json: async () => responses[path]};
  };
  const pane = vm.createContext({
    Office: office, window: {Office: office, addEventListener() {}}, Word, document, fetch,
    crypto: webcrypto, TextEncoder, setTimeout, clearTimeout
  });
  for (const script of scripts) vm.runInContext(script, pane);
  nodes.token.value = "synthetic";
  await nodes.connect.events.submit({preventDefault() {}});
  if (fileChanged) file = "/synthetic/B/contract.docx";
  const writeButton = () => nodes.suggestions.querySelectorAll("button").find(button => button.dataset.write === "true");
  const button = writeButton();
  assert.ok(button, nodes.status.textContent);
  await button.events.click();
  return {writes, status: nodes.status.textContent, disabled: writeButton()?.disabled};
}

(async () => {
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
