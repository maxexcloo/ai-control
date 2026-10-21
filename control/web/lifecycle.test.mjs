import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8').split("$('login-form').addEventListener")[0];
function application({ liveRendering = false } = {}) {
  class Element {
    children = []; dataset = {}; events = {}; hidden = true; open = false;
    constructor(tag) { this.tag = tag; }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    replaceChildren(...children) {
      let focused = document.activeElement;
      while (focused) { if (focused === this) { document.activeElement = document.body; break; } focused = focused.parent; }
      this.children = []; this.append(...children);
    }
    setAttribute() {}
    closest(selector) {
      if (selector === '#live-gpus wa-button[data-runtime]') return this.tag === 'wa-button' && this.dataset.runtime ? this : null;
      let node = this;
      while (node) { if (selector === '.gpu-status' && node.className?.split(' ').includes('gpu-status')) return node; node = node.parent; }
      return null;
    }
    focus() { document.activeElement = this; }
    remove() { this.parent.children = this.parent.children.filter((node) => node !== this); }
    querySelectorAll(tag) { return this.children.filter((node) => node.tag === tag); }
    addEventListener(event, handler) { this.events[event] = handler; }
  }
  const nodes = new Map();
  const document = {
    addEventListener() {},
    body: {},
    createElement: (tag) => new Element(tag),
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); },
    querySelectorAll(selector) {
      if (selector !== '#live-gpus wa-button[data-runtime]') return [];
      const visit = (node) => [node, ...node.children.flatMap(visit)];
      return visit(document.getElementById('live-gpus')).filter((node) => node.tag === 'wa-button' && node.dataset.runtime);
    }
  };
  const responses = new Map([
    ['session', {}], ['catalogue', { rules: { workloads: {} }, models: [] }],
    ['status', { runtimes: {} }], ['jobs', []]
  ]);
  const requests = [];
  const context = vm.createContext({
    document, requestAnimationFrame: (callback) => callback(),
    fetch: async (url) => {
      const path = url.replace('/api/', ''); requests.push(path);
      const value = responses.get(path);
      if (value instanceof Error) throw value;
      return { ok: !value?.error, status: value?.status || 200, text: async () => JSON.stringify(value) };
    }
  });
  vm.runInContext(source, context);
  // Keep these tests on connection/history behaviour; board rendering has its own suite.
  vm.runInContext('renderTray = () => {}; preview = async () => {};', context);
  if (liveRendering) vm.runInContext('renderHost = () => {}; renderBoardTemperatures = () => {}; renderBoard = () => {}; initialised = true;', context);
  else vm.runInContext('renderStatus = (data) => { live = data; initialised = true; };', context);
  return {
    node: (id) => document.getElementById(id), responses, requests, document,
    run: (code) => vm.runInContext(code, context)
  };
}

test('initial connection failure offers Retry and recovers without reloading the page', async () => {
  const app = application();
  app.responses.set('session', new Error('Offline'));
  await app.run('load()');
  assert.equal(app.node('connection-state').hidden, false);
  assert.equal(app.node('retry-connection').hidden, false);
  assert.equal(app.node('connection-progress').hidden, true);
  assert.equal(app.node('dashboard').hidden, true);
  app.responses.set('session', {});
  await app.node('retry-connection').events.click();
  assert.equal(app.node('connection-state').hidden, true);
  assert.equal(app.node('dashboard').hidden, false);
  assert.equal(app.node('error').hidden, true);
});

test('an expired session shows Sign In without duplicate connection or error alerts', async () => {
  const app = application();
  app.responses.set('session', { error: 'Sign in required', status: 401 });
  await app.run('load()');
  assert.equal(app.node('login').hidden, false);
  assert.equal(app.node('dashboard').hidden, true);
  assert.equal(app.node('connection-state').hidden, true);
  assert.equal(app.node('error').hidden, true);
});

test('live connection failures and recovery use one connection callout', async () => {
  const app = application();
  await app.run('load()');
  app.responses.set('status', new Error('Offline'));
  assert.equal(await app.run('refresh()'), false);
  assert.equal(app.node('connection-state').hidden, false);
  assert.equal(app.node('apply-board').disabled, true);
  assert.equal(app.node('error').hidden, true);
  app.responses.set('status', { runtimes: {} });
  assert.equal(await app.run('refresh()'), true);
  assert.equal(app.node('connection-state').hidden, true);
});

test('history polling keeps unchanged and open-menu items intact', async () => {
  const app = application();
  const job = { id: 'job', state: 'running', action: 'check', started_at: 1 };
  app.responses.set('jobs', [job]);
  await app.run('loadHistory()');
  const menu = app.node('recent-operations'); const original = menu.children[0];
  await app.run('loadHistory()');
  assert.equal(menu.children[0], original);
  menu.open = true; app.responses.set('jobs', [{ ...job, state: 'completed' }]);
  await app.run('loadHistory()');
  assert.equal(menu.children[0], original);
  menu.open = false; await app.run('loadHistory()');
  assert.notEqual(menu.children[0], original);
  assert.match(menu.children[0].children[0].textContent, /Completed/);
});

test('polling retains Restart focus on the same GPU when a workload spans both GPUs', () => {
  const app = application({ liveRendering: true });
  app.run(`
    catalogue = { models: [{ id: 'dual-text', label: 'Dual Text' }], rules: { workloads: { 'dual-text': { kind: 'text', runtime: 'text' } } } };
    var status = { updated: '2026-10-21T00:00:00Z', runtimes: { text: { profile: 'dual-text', devices: ['0', '1'], running: true, health: 'healthy' } } };
    renderStatus(status);
  `);
  const buttons = () => app.document.querySelectorAll('#live-gpus wa-button[data-runtime]');
  buttons()[1].focus();
  app.run('renderStatus(status)');
  assert.equal(app.document.activeElement, buttons()[1]);
  assert.equal(app.document.activeElement.closest('.gpu-status').dataset.device, '1');
});
