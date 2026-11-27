// The pane speaks to two external boundaries: desktop Word and the bound review service.
const assert = require("node:assert/strict");
const collection = items => ({items, load() {return this;}});
const outsideControl = {isNullObject: true, load() {}};
const current = runs => runs.filter(r => r.type !== "Deleted").map(r => r.text).join("");
const original = runs => runs.filter(r => r.type !== "Added").map(r => r.text).join("");

function revisionService(items, options = {}) {
  const operations = [];
  const state = {reviewId: "A", title: "合成 A", documentName: "contract.docx", receiver: {state: "unconfigured"}};
  const reply = data => ({ok: true, json: async () => JSON.parse(JSON.stringify(data))});
  async function fetch(path, request) {
    const body = JSON.parse(request.body);
    switch (path) {
      case "/api/bind": return reply({...state, suggestions: items});
      case "/api/status": return reply(state);
      case "/api/suggestion": return reply({reviewId: "A", suggestion: items.find(i => i.id === body.id)});
      case "/api/word/list": return reply({suggestions: items, pendingBatches: [], revisionOperations: operations});
      case "/api/revision-operations/plan": {
        const batch = String(operations.length + 1);
        for (const id of body.items) {
          operations.push({batch, id, item: items.find(i => i.id === id), plan: null, receipt: null, status: "unexecuted"});
        }
        return reply({batch});
      }
      case "/api/revision-operations/begin": {
        const op = operations.find(o => o.batch === body.batch && o.id === body.id);
        if (op.status !== "unexecuted") return reply({mayWrite: false});
        Object.assign(op, {plan: body.plan, status: "unknown"});
        options.afterBegin?.(op);
        return reply({mayWrite: true, permit: "synthetic-permit"});
      }
      case "/api/revision-operations/observe": {
        const op = operations.find(o => o.batch === body.batch && o.id === body.id);
        options.beforeObserve?.(op, body);
        Object.assign(op, {status: body.status, receipt: body.status === "written" ? body.after : op.receipt});
        return reply({});
      }
      default: throw Error("Unexpected task pane request: " + path);
    }
  }
  return {fetch};
}

function paragraph(initial, word) {
  let runs = [{text: initial}];
  function replace(start, end, text) {
    let offset = 0;
    for (const [index, run] of runs.entries()) {
      if (run.type === "Deleted") continue;
      const limit = offset + run.text.length;
      if (!run.type && start >= offset && end <= limit) {
        const left = run.text.slice(0, start - offset);
        const removed = run.text.slice(start - offset, end - offset);
        const right = run.text.slice(end - offset);
        runs.splice(index, 1, ...[
          {text: left}, {type: "Deleted", text: removed, author: "乙"},
          {type: "Added", text, author: "乙"}, {text: right}
        ].filter(r => r.text));
        word.writes.push([removed, text]);
        return;
      }
      offset = limit;
    }
    throw Error("The synthetic Word fixture expects a target in one unmodified run.");
  }
  function range(start, end, before, after) {
    const value = current(runs).slice(start, end);
    return {
      start, end, text: after ?? value, load() {return this;},
      getReviewedText: state => ({value: state === "Original" ? (before ?? value) : (after ?? value)}),
      getRange: edge => edge === "Start" ? range(start, start) : range(end, end),
      expandTo: other => range(Math.min(start, other.start), Math.max(end, other.end)),
      compareLocationWith: other => ({value: start === other.start && end === other.end ? "Equal"
        : end < other.start ? "Before" : start > other.end ? "After" : end === other.start ? "AdjacentBefore"
        : start === other.end ? "AdjacentAfter" : "OverlapsBefore"}),
      getComments: () => collection([]),
      select() { word.selection = this; },
      insertText(text, location) {
        if (location === "Replace") replace(start, end, text);
        else if (location === "Start") replace(start, start, text);
        else throw Error("Unexpected insertion mode: " + location);
      },
      delete() {replace(start, end, "");}
    };
  }
  return {
    parentContentControlOrNullObject: outsideControl, parentTableCellOrNullObject: outsideControl,
    get current() {return current(runs);},
    get revisions() {return runs.filter(r => r.type).map(r => [r.type, r.text, r.author]);},
    getRange() {
      const text = current(runs);
      return {...range(0, text.length, original(runs), text), getOoxml: () => ({value: JSON.stringify(runs)}),
        search(needle) {
          const found = [];
          for (let i = text.indexOf(needle); i >= 0; i = text.indexOf(needle, i + needle.length)) {
            found.push(range(i, i + needle.length));
          }
          return collection(found);
        }
      };
    },
    getTrackedChanges() {
      let offset = 0;
      const tracked = [];
      for (const run of runs) {
        const start = offset;
        if (run.type !== "Deleted") offset += run.text.length;
        if (run.type) {
          const deleted = run.type === "Deleted";
          const changeRange = range(start, offset, deleted ? run.text : "", deleted ? "" : run.text);
          tracked.push({...run, getRange: () => changeRange});
        }
      }
      return collection(tracked);
    },
    undo() { runs = [{text: initial}]; },
    appendText(text) { runs.push({type: "Added", text, author: "丙"}); }
  };
}

async function batchScenario(scenario, openPane) {
  const word = {writes: []};
  const body = paragraph("须在五日内通知甲方。", word);
  const cell = paragraph("保留纸本。", word);
  const items = [
    {id: "days", from: "五日", to: "十个工作日", paragraph: body.current, bodyParagraph: 0},
    {id: "party", from: "甲方", to: "乙方", paragraph: body.current, bodyParagraph: 0},
    {id: "copy", from: "纸本", to: "原件", paragraph: cell.current, table: 0, row: 0, column: 1}
  ];
  const table = {
    load() {}, isUniform: true, nestingLevel: 1, parentContentControlOrNullObject: outsideControl,
    getCell(row, column) {
      assert.deepEqual([row, column], [0, 1]);
      return {body: {paragraphs: collection([cell])}};
    }
  };
  const context = {sync: async () => {}, document: {load() {}, changeTrackingMode: "TrackAll", isReadOnly: false,
    protectionType: "NoProtection", getSelection: () => word.selection,
    body: {paragraphs: collection([body]), tables: collection([table]), getRange: () => ({getComments: () => collection([])})}
  }};
  const Word = {run: action => action(context)};
  const options = {};
  if (scenario === "overlap") {
    items.push({...items[0], id: "period", from: "五日内", to: "本周"});
    items.push({...items[2], id: "missing", from: "附件", to: "清单"});
  }
  if (scenario === "reconnect") {
    items.push({...items[0], id: "must", from: "须", to: "应"});
    options.beforeObserve = (_op, observation) => {
      if (observation.status === "written") {
        delete options.beforeObserve;
        throw Error("Synthetic network loss before the service receives the first Word receipt.");
      }
    };
  }
  if (scenario === "manual") {
    options.afterBegin = op => {
      if (op.id === "party") body.appendText("手工补充。");
    };
  }
  const service = revisionService(items, options);
  const reopen = () => openPane(Word, service.fetch);
  let nodes = await reopen();
  function row(id) {
    const line = nodes.suggestions.children.find(li => li.querySelectorAll("input").some(i => i.dataset.id === id));
    assert.ok(line, "Suggestion row for " + id + ": " + nodes.status.textContent);
    return {
      text: line.querySelectorAll("p").map(p => p.textContent).join(" "),
      choice: line.querySelectorAll("input").find(i => i.dataset.id === id && i.type === "checkbox"),
      write: line.querySelectorAll("button").find(b => b.dataset.write === "true")
    };
  }
  async function write(ids) {
    for (const id of ids) {
      const box = row(id).choice;
      assert.ok(box && !box.disabled, "An unexecuted suggestion can be selected: " + id);
      box.checked = true;
      await box.events.change({target: box});
    }
    const button = nodes["write-selected"];
    assert.ok(button && !button.disabled, "The selected suggestions can be written as a batch.");
    await button.events.click();
  }
  function expectLocked(id, state) {
    assert.match(row(id).text, state);
    assert.equal(row(id).choice.disabled, true, id);
    assert.equal(row(id).write.disabled, true, id);
  }
  if (scenario === "overlap") {
    await write(items.map(item => item.id));
    for (const id of ["days", "period"]) assert.match(row(id).text, /重叠/);
    assert.match(row("missing").text, /定位/);
    assert.deepEqual(word.writes, []);
    assert.deepEqual([body.current, cell.current], ["须在五日内通知甲方。", "保留纸本。"]);
    assert.deepEqual([...body.revisions, ...cell.revisions], []);
    return;
  }
  if (scenario === "undo") {
    await write(["days"]);
    expectLocked("days", /已写入/);
    body.undo();
    nodes = await reopen();
    expectLocked("days", /需核对/);
    await nodes.refresh.events.click();
    expectLocked("days", /需核对/);
    assert.equal(body.current, "须在五日内通知甲方。");
    assert.deepEqual(body.revisions, []);
    assert.deepEqual(word.writes, [["五日", "十个工作日"]]);
    return;
  }
  await write(items.map(item => item.id));
  if (scenario === "manual") {
    assert.deepEqual(word.writes, [["五日", "十个工作日"]]);
    assert.equal(body.current, "须在十个工作日内通知甲方。手工补充。");
    assert.deepEqual(body.revisions, [
      ["Deleted", "五日", "乙"], ["Added", "十个工作日", "乙"], ["Added", "手工补充。", "丙"]
    ]);
    assert.equal(cell.current, "保留纸本。");
    for (const id of ["party", "copy"]) {
      assert.match(row(id).text, /未执行/);
      assert.equal(row(id).choice.disabled, false);
    }
    return;
  }
  if (scenario === "reconnect") {
    assert.deepEqual(word.writes, [["五日", "十个工作日"]]);
    nodes = await reopen();
    expectLocked("days", /已写入/);
    for (const id of ["party", "copy", "must"]) {
      assert.match(row(id).text, /未执行/);
      assert.equal(row(id).choice.disabled, false);
    }
    // The user resumes in a different order from the original selection.
    await write(["must"]);
    await write(["party", "copy"]);
    await nodes.refresh.events.click();
    for (const {id} of items) expectLocked(id, /已写入/);
    assert.equal(body.current, "应在十个工作日内通知乙方。");
    assert.deepEqual(body.revisions, [
      ["Deleted", "须", "乙"], ["Added", "应", "乙"],
      ["Deleted", "五日", "乙"], ["Added", "十个工作日", "乙"],
      ["Deleted", "甲方", "乙"], ["Added", "乙方", "乙"]
    ]);
    assert.equal(cell.current, "保留原件。");
    assert.deepEqual(cell.revisions, [["Deleted", "纸本", "乙"], ["Added", "原件", "乙"]]);
    assert.deepEqual(word.writes, [["五日", "十个工作日"], ["须", "应"], ["甲方", "乙方"], ["纸本", "原件"]]);
    return;
  }
  assert.equal(body.current, "须在十个工作日内通知乙方。", nodes.status.textContent);
  assert.equal(cell.current, "保留原件。");
  assert.deepEqual(body.revisions, [
    ["Deleted", "五日", "乙"], ["Added", "十个工作日", "乙"],
    ["Deleted", "甲方", "乙"], ["Added", "乙方", "乙"]
  ]);
  assert.deepEqual(cell.revisions, [["Deleted", "纸本", "乙"], ["Added", "原件", "乙"]]);
  await nodes.refresh.events.click();
  for (const id of ["days", "party", "copy"]) expectLocked(id, /已写入/);
  assert.deepEqual(word.writes, [["五日", "十个工作日"], ["甲方", "乙方"], ["纸本", "原件"]]);
}
module.exports = {revisionService, batchScenario};
