// Synthetic Word and task pane boundaries. Click the shipped controls with tracking off.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {createHash, webcrypto} = require("node:crypto");
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
const cancelled = new Set(), cancelRequests = [], storage = new Map();
const sessionStorage = {
  getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)
};
let selection, afterBegin, documentFailure, cancelOffline = false, nativeThrows = false, losesReceipt = false, nextId = 10;
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
      if (nativeThrows) throw Error("synthetic native error");
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
  window: {sessionStorage}, document, byId, crypto: webcrypto, TextEncoder, setTimeout,
  MutationObserver: class { observe() {} },
  binding: {reviewId: "synthetic", key: "file", canonicalKey: "canonical-file", token: "demo"},
  run: async action => action(), message() {}, refreshConnection: async () => {},
  checkDocument: async () => {
    if (documentFailure) {
      const fail = documentFailure;
      documentFailure = null;
      fail();
    }
  },
  Word: {run: async action => action({document: wordDoc, sync: async () => {}})},
  async request(path, token, data) {
    if (path.endsWith("/list")) return {operations: [...journal]};
    if (path.endsWith("/begin")) {
      const id = data.permitHash;
      assert.match(id, /^[0-9a-f]{64}$/);
      assert.ok(!cancelled.has(id));
      journal.push({...data, id, receipt: null});
      if (afterBegin) {
        const change = afterBegin;
        afterBegin = null;
        change();
      }
      return {id, mayWrite: true};
    }
    if (path.endsWith("/cancel")) {
      cancelRequests.push([token, data.reviewId, data.documentKey, data.id]);
      if (cancelOffline) throw Error("synthetic offline");
      assert.equal(createHash("sha256").update(data.permit).digest("hex"), data.id);
      const index = journal.findIndex(op => op.id === data.id);
      if (index !== -1) {
        assert.equal(journal[index].receipt, null);
        journal.splice(index, 1);
      }
      cancelled.add(data.id);
      return {id: data.id};
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
const loadPane = () => vm.runInContext(pane.match(/<script>([\s\S]*?)<\/script>/)[1], ctx);
loadPane();
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
  afterBegin = () => { selection = wordRange(2, 4); };  // Identical text at the other occurrence.
  await assert.rejects(post, /目标文字的位置已改变/);
  assert.equal(threads.length, 1);
  assert.equal(journal.length, 0);
  selection = wordRange(10, 12);
  afterBegin = () => { throw Error("synthetic lost begin response"); };
  await assert.rejects(post, /lost begin response/);
  assert.equal(journal.length, 0);
  await post();
  assert.equal(threads[1].getRange().text, "五日");
  assert.equal(threads[1].getRange().start, 10);
  assert.equal(threads[1].authorName, "当前作者");
  draft("给乙的回复");
  const original = ctx.binding;
  afterBegin = () => {
    documentFailure = () => {
      cancelOffline = true;
      ctx.binding = null;
      throw Error("synthetic disconnection");
    };
  };
  await assert.rejects(button(0, "回复此线程").click, /未向 Word 提交/);
  assert.equal(oldThread.replies.items.length, 1);
  assert.equal(storage.size, 1);
  assert.deepEqual(cancelRequests.at(-1).slice(0, 3), ["demo", "synthetic", "file"]);
  const interrupted = journal.at(-1).id, cancellations = cancelRequests.length;
  cancelOffline = false;
  loadPane();  // Same Word pane session reloaded; no in-memory cancellation proof survives.
  ctx.binding = {reviewId: "other", key: "file", canonicalKey: "canonical-file", token: "other-token"};
  await refresh();
  assert.equal(cancelRequests.length, cancellations);
  ctx.binding = {...original, key: "file-alias", token: "new-demo"};
  await refresh();
  assert.deepEqual(cancelRequests.at(-1), ["new-demo", "synthetic", "file-alias", interrupted]);
  assert.equal(storage.size, 0);
  await button(0, "回复此线程").click();
  assert.deepEqual(oldThread.replies.items.map(r => [r.content, r.authorName]), [["旧回复", "另一人"], ["给乙的回复", "当前作者"]]);
  assert.equal(threads[1].replies.items.length, 0);
  afterBegin = () => { oldThread.resolved = true; };
  await assert.rejects(button(0, "解决此线程").click, /原线程已改变/);
  assert.ok(!journal.some(op => op.kind === "resolve"));
  oldThread.resolved = false;
  await button(0, "解决此线程").click();
  assert.equal(oldThread.resolved, true);
  await button(0, "重开此线程").click();
  assert.equal(oldThread.resolved, false);
  oldThread.resolved = true;
  await refresh();
  assert.ok(button(0, "重开此线程"));
  oldThread.resolved = false;
  await refresh();

  const beforeNativeError = cancelRequests.length;
  draft("原生调用中断");
  nativeThrows = true;
  await assert.rejects(post, /需核对/);
  nativeThrows = false;
  await assert.rejects(post, /需核对/);
  assert.equal(cancelRequests.length, beforeNativeError);
  assert.equal(threads.filter(thread => thread.content === "原生调用中断").length, 1);
  threads.pop();

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
