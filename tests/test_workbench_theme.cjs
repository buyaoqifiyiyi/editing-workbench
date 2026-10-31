/* Run the real blocking theme script without a browser, server, or model. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../src/jianying_engine/web/theme.js'), 'utf8');
const key = 'workbench-theme';

function load({store = new Map(), readFails = false, writeFails = false, accessFails = false,
               initialTheme, fetchImpl = async () => ({ok: true})} = {}) {
  const elements = new Map();
  const events = new Map();
  function element() {
    return {
      textContent: '', title: '', attributes: {}, listeners: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
    };
  }
  const document = {
    documentElement: {dataset: {theme: initialTheme}},
    querySelector(selector) {
      assert.equal(selector, 'meta[name="workbench-token"]');
      return {content: 'private-test-token'};
    },
    getElementById(id) { return elements.get(id) || null; },
    addEventListener(name, callback) { events.set(name, callback); },
  };
  const requests = [];
  const context = {document, fetch(url, options) {
    requests.push({url, options});
    return fetchImpl(url, options);
  }};
  Object.defineProperty(context, 'localStorage', {get() {
    if (accessFails) throw new Error('storage access blocked');
    return {
      getItem(name) {
        if (readFails) throw new Error('storage read blocked');
        return store.get(name) ?? null;
      },
      setItem(name, value) {
        if (writeFails) throw new Error('storage write blocked');
        store.set(name, value);
      },
    };
  }});
  vm.runInNewContext(source, context, {filename: 'theme.js'});
  return {
    document, store, requests,
    flush() { return new Promise(resolve => setImmediate(resolve)); },
    title() { return elements.get('theme-toggle').title; },
    ready() {
      for (const id of ['theme-toggle', 'theme-icon', 'theme-label']) elements.set(id, element());
      events.get('DOMContentLoaded')();
    },
    click() { elements.get('theme-toggle').listeners.click(); },
    assertTheme(theme) {
      const dark = theme === 'dark';
      assert.equal(document.documentElement.dataset.theme, theme);
      assert.equal(elements.get('theme-label').textContent, dark ? '深色' : '浅色');
      assert.equal(elements.get('theme-icon').attributes.href, dark ? '#i-sun' : '#i-moon');
      const action = dark ? '切换到浅色主题' : '切换到深色主题';
      assert.equal(elements.get('theme-toggle').title, action);
      assert.equal(elements.get('theme-toggle').attributes['aria-label'], action);
    },
  };
}

test('default is applied before the header exists; both toggles update labels, icons and ARIA', () => {
  const page = load();
  assert.equal(page.document.documentElement.dataset.theme, 'dark');
  assert.equal(page.store.has(key), false);
  page.ready();
  page.assertTheme('dark');
  page.click();
  page.assertTheme('light');
  assert.equal(page.store.get(key), 'light');
  page.click();
  page.assertTheme('dark');
  assert.equal(page.store.get(key), 'dark');
});

test('server-injected theme wins over localStorage, including a fresh private browser', () => {
  for (const initialTheme of ['dark', 'light']) {
    for (const store of [new Map(), new Map([[key, initialTheme === 'dark' ? 'light' : 'dark']])]) {
      const page = load({initialTheme, store});
      assert.equal(page.document.documentElement.dataset.theme, initialTheme);
      page.ready();
      page.assertTheme(initialTheme);
      assert.equal(page.requests.length, 0);
    }
  }
  const fallback = load({initialTheme: '__WORKBENCH_THEME__', store: new Map([[key, 'light']])});
  assert.equal(fallback.document.documentElement.dataset.theme, 'light');
});

test('rapid switches render immediately and save authenticated requests strictly in order', async () => {
  const pending = [];
  const page = load({fetchImpl: () => new Promise(resolve => pending.push(resolve))});
  page.ready();
  page.click();
  page.click();
  page.assertTheme('dark');
  await page.flush();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].options.body, JSON.stringify({theme: 'light'}));
  pending.shift()({ok: true});
  await page.flush();
  assert.equal(page.requests.length, 2);
  const request = page.requests[1];
  assert.equal(request.url, '/api/theme');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['X-Workbench-Token'], 'private-test-token');
  assert.equal(request.options.headers['Content-Type'], 'application/json');
  assert.equal(request.options.body, JSON.stringify({theme: 'dark'}));
  pending.shift()({ok: true});
  await page.flush();
  page.assertTheme('dark');
});

test('HTTP and network failures warn without exposing token or preventing the next save', async () => {
  for (const fail of [async () => ({ok: false}), async () => { throw new Error('private-test-token'); }]) {
    let attempts = 0;
    const page = load({fetchImpl: (...args) => ++attempts === 1 ? fail(...args) : Promise.resolve({ok: true})});
    page.ready();
    page.click();
    await page.flush();
    assert.equal(page.document.documentElement.dataset.theme, 'light');
    assert.match(page.title(), /仅本次有效/);
    assert.doesNotMatch(page.title(), /private-test-token/);
    page.click();
    await page.flush();
    page.assertTheme('dark');
    assert.equal(attempts, 2);
  }
});

test('an older failed request cannot change the latest selection warning', async () => {
  const pending = [];
  const page = load({fetchImpl: () => new Promise(resolve => pending.push(resolve))});
  page.ready();
  page.click();
  await page.flush();
  page.click();
  pending.shift()({ok: false});
  await page.flush();
  page.assertTheme('dark');
  pending.shift()({ok: false});
  await page.flush();
  assert.match(page.title(), /仅本次有效/);
});

test('each saved selection survives reload and is applied before DOMContentLoaded', () => {
  const store = new Map();
  let page = load({store});
  page.ready();
  page.click();
  page = load({store});
  assert.equal(page.document.documentElement.dataset.theme, 'light');
  page.ready();
  page.assertTheme('light');
  page.click();
  page = load({store});
  assert.equal(page.document.documentElement.dataset.theme, 'dark');
  page.ready();
  page.assertTheme('dark');
});

test('unknown stored values default to dark and become valid on selection', () => {
  for (const value of ['auto', '', 'LIGHT', 'unknown']) {
    const page = load({store: new Map([[key, value]])});
    assert.equal(page.document.documentElement.dataset.theme, 'dark');
    page.ready();
    page.assertTheme('dark');
    page.click();
    page.assertTheme('light');
    assert.equal(page.store.get(key), 'light');
  }
});

test('read exceptions fall back to dark without preventing switching or later writes', () => {
  const page = load({store: new Map([[key, 'light']]), readFails: true});
  page.ready();
  page.assertTheme('dark');
  page.click();
  page.assertTheme('light');
  assert.equal(page.store.get(key), 'light');
});

test('write exceptions preserve the current in-memory choice for repeated toggles', () => {
  const page = load({store: new Map([[key, 'light']]), writeFails: true});
  page.ready();
  page.assertTheme('light');
  page.click();
  page.assertTheme('dark');
  assert.equal(page.store.get(key), 'light');
  page.click();
  page.assertTheme('light');
});

test('blocked localStorage property access leaves both in-memory themes usable', () => {
  const page = load({accessFails: true});
  page.ready();
  page.assertTheme('dark');
  page.click();
  page.assertTheme('light');
  page.click();
  page.assertTheme('dark');
});
