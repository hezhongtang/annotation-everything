// Synthetic Word and task pane boundaries. Click the shipped controls with tracking off.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {webcrypto} = require("node:crypto");
const vm = require("node:vm");

class Element {
  constructor() { this.children = []; this.value = ""; }
  addEventListener(name, action) { this[name] = action; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
const nodes = new Map();
const byId = id => {
  if (!nodes.has(id)) nodes.set(id, new Element());
  return nodes.get(id);
};
const document = {
  addEventListener: (_, init) => init(),
  createElement: () => new Element()
};
const text = "应于五日内回复。应于五日内回复。";
const threads = [];
const journal = [];
let selection, losesReceipt = false, nextId = 10;
function collection(items) { return {items, load() { return this; }}; }
function wordRange(start, end) {
  return {
    text: text.slice(start, end), start,
    load() { return this; },
    getRange() { return wordRange(start, start); },
    expandTo(other) { return wordRange(start, other.start); },
    insertComment(content) {
      const thread = comment(String(nextId++), content, "当前作者", this);
      threads.push(thread);
      return thread;
    }
  };
}
function entry(id, content, authorName) {
  return {id, content, authorName, creationDate: new Date("2026-11-22T12:00:00Z"), load() { return this; }};
}
function comment(id, content, authorName, range) {
  return {
    ...entry(id, content, authorName), resolved: false, replies: collection([]),
    getRange() { return range; },
    reply(content) {
      const reply = entry(String(nextId++), content, "当前作者");
      this.replies.items.push(reply);
      return reply;
    }
  };
}
const oldThread = comment("1", "请核定原时限", "乙", wordRange(2, 4));
oldThread.replies.items.push(entry("2", "旧回复", "另一人"));
threads.push(oldThread);
const wordDoc = {
  changeTrackingMode: "Off", getSelection: () => selection,
  body: {getRange: () => wordRange(0, 0), getComments: () => collection(threads)}
};
const ctx = vm.createContext({
  window: {}, document, byId, crypto: webcrypto, TextEncoder, setTimeout,
  MutationObserver: class { observe() {} },
  binding: {reviewId: "synthetic", key: "file", token: "demo"},
  run: async action => action(), message() {}, checkDocument: async () => {}, refreshConnection: async () => {},
  Word: {run: async action => action({document: wordDoc, sync: async () => {}})},
  async request(path, _, data) {
    if (path.endsWith("/list")) return {operations: journal};
    if (path.endsWith("/begin")) {
      const id = String(journal.length + 1);
      journal.push({...data, id, receipt: null});
      return {id, mayWrite: true};
    }
    if (path.endsWith("/confirm")) {
      if (losesReceipt) throw Error("synthetic connection loss");
      journal.find(op => op.id === data.id).receipt = data;
      return {};
    }
    throw Error("unexpected API " + path);
  }
});
const pane = readFileSync(process.argv[2], "utf8");
vm.runInContext(pane.match(/<script>([\s\S]*?)<\/script>/)[1], ctx);
const button = (index, label) => byId("comment-threads").children[index].children.find(node => node.textContent === label);
const draft = value => { byId("comment-draft").value = value; };
const refresh = () => byId("comment-refresh").click();
const post = () => byId("comment-post").click();

(async () => {
  await refresh();
  selection = wordRange(2, 2);
  draft("合成意见");
  await assert.rejects(post, /选中/);
  assert.equal(journal.length, 0);
  selection = wordRange(10, 12);
  await post();
  assert.equal(threads[1].getRange().text, "五日");
  assert.equal(threads[1].getRange().start, 10);
  assert.equal(threads[1].authorName, "当前作者");
  draft("给乙的回复");
  await button(0, "回复此线程").click();
  assert.deepEqual(oldThread.replies.items.map(r => [r.content, r.authorName]), [["旧回复", "另一人"], ["给乙的回复", "当前作者"]]);
  assert.equal(threads[1].replies.items.length, 0);
  await button(0, "解决此线程").click();
  assert.equal(oldThread.resolved, true);
  await button(0, "重开此线程").click();
  assert.equal(oldThread.resolved, false);
  oldThread.resolved = true;
  await refresh();
  assert.ok(button(0, "重开此线程"));
  oldThread.resolved = false;
  await refresh();

  losesReceipt = true;
  draft("仅发一条");
  await assert.rejects(button(0, "回复此线程").click, /需核对/);
  await refresh();
  await assert.rejects(button(0, "回复此线程").click, /需核对/);
  assert.equal(oldThread.replies.items.filter(r => r.content === "仅发一条").length, 1);
  const staleButton = button(0, "回复此线程");
  threads.shift();
  draft("失效目标");
  await assert.rejects(staleButton.click, /原线程已改变/);
  assert.equal(threads.length, 1);
  // A Word undo removed the successful post; a new selection must not silently repeat it.
  threads.pop();
  await refresh();
  selection = wordRange(2, 4);
  draft("合成意见");
  await assert.rejects(post, /需核对/);
  assert.equal(threads.length, 0);
})().catch(err => { console.error(err); process.exitCode = 1; });
