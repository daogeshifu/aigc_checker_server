// Run with: node --test tests/test_tasks_page.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/tasks.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function page() {
  const nodes = new Map(), events = new Map(), intervals = [];
  function element(selector) {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {
        value: '', textContent: '', innerHTML: '', disabled: false, hidden: false,
        tagName: selector === '#searchInput' ? 'INPUT' : 'SELECT',
        classList: {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)},
        addEventListener() {}, replaceChildren() { this.innerHTML = ''; },
        querySelectorAll: () => [], querySelector: () => null,
      });
    }
    return nodes.get(selector);
  }
  ['#sourceFilter', '#typeFilter', '#statusFilter'].forEach(id => element(id).value = 'all');
  const requests = [];
  let respond = async () => ({items: [], total: 0, retention_limit: 300, can_view_all_tasks: true});
  const context = vm.createContext({
    document: {hidden: false, querySelector: element, querySelectorAll: () => [], addEventListener() {}},
    window: {addEventListener: (name, callback) => events.set(name, callback)},
    setTimeout: () => 1, clearTimeout() {}, setInterval: callback => intervals.push(callback),
    AbortController, Intl, URLSearchParams,
    fetch: async (url, options) => {
      requests.push({url, options});
      return {ok: true, status: 200, json: async () => respond(url, options)};
    },
  });
  vm.runInContext(script, context);
  return {
    element, requests, intervals, events, context,
    respond: callback => { respond = callback; },
    start: () => events.get('DOMContentLoaded')(),
    run: code => vm.runInContext(code, context),
  };
}

const task = id => ({task_id: id, task_type: 'scan', status: 'success', source: 'api', duration_ms: 100});

test('auto refresh discovers new tasks after a completed or empty list', async () => {
  for (const initial of [[], [task('scan-completed')]]) {
    const ui = page();
    let items = initial;
    ui.respond(async () => ({items, total: items.length, retention_limit: 200, can_view_all_tasks: true}));
    ui.start();
    await new Promise(resolve => setImmediate(resolve));
    items = [task('scan-new'), ...initial];
    ui.intervals[0]();
    // Await the in-flight fetch, JSON read and list update.
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ui.requests.length, 2);
    assert.match(ui.element('#tableWrap').innerHTML, /scan-new/);
    assert.equal(ui.requests[1].options.cache, 'no-store');
    assert.equal(ui.requests[1].options.headers, undefined);
  }
});

test('loads and searches retained tasks beyond the first 200', async () => {
  const ui = page();
  const items = Array.from({length: 205}, (_, index) => task(`scan-${index}`));
  ui.respond(async url => {
    const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
    return {items: items.slice(offset, offset + 200), total: items.length, retention_limit: 300, can_view_all_tasks: true};
  });
  await ui.run('loadTasks()');
  assert.equal(ui.requests.length, 2);
  assert.match(ui.requests[1].url, /offset=200/);
  assert.equal(ui.element('#totalCount').textContent, 205);
  ui.element('#searchInput').value = 'scan-204';
  ui.run('applyFilters()');
  assert.match(ui.element('#tableWrap').innerHTML, /scan-204/);
  assert.match(ui.element('#scopeNote').textContent, /300/);
});

test('background refresh preserves the current page and skips hidden tabs', async () => {
  const ui = page();
  const items = Array.from({length: 25}, (_, index) => task(`scan-${index}`));
  ui.respond(async () => ({items, total: items.length, can_view_all_tasks: true}));
  ui.start();
  await new Promise(resolve => setImmediate(resolve));
  ui.run('state.page=2');
  await ui.run('loadTasks(true)');
  assert.match(ui.element('#pageInfo').textContent, /第 2/);
  assert.match(ui.element('#scopeNote').textContent, /全部调用方 · 无需 Token/);
  ui.context.document.hidden = true;
  ui.intervals[0]();
  assert.equal(ui.requests.length, 2);
});

test('page and task details work without stored credentials', async () => {
  const ui = page();
  ui.respond(async () => task('scan-public'));
  await ui.run("openDetail('scan-public')");
  assert.equal(ui.requests[0].url, '/api/tasks/scan-public');
  assert.equal(ui.requests[0].options.headers, undefined);
  assert.match(ui.element('#modalBody').innerHTML, /完整执行步骤/);
  assert.doesNotMatch(html, /taskToken|sessionStorage|Authorization/);
});

test('embedded history loads without a token store', async () => {
  const ui = page();
  const items = [task('scan-public-history')];
  ui.respond(async () => ({items, total: 1, retention_limit: 200, can_view_all_tasks: true}));
  for (const filename of ['execution-log.js', 'task-history.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../web', filename), 'utf8'), ui.context);
  }
  ui.run('window.createTaskHistory({})');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].options.headers, undefined);
  assert.match(ui.element('#historyTable').innerHTML, /scan-public-history/);
  assert.match(ui.element('#historyScope').textContent, /无需 Token/);
});
