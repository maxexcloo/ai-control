import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the real board handlers without starting polling or making API requests.
const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8').split("$('login-form').addEventListener")[0];
function board(allocation, { coarse = false } = {}) {
  class Element {
    children = []; dataset = {}; events = {}; disabled = false;
    classList = { contains: () => false };
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute() {}
    addEventListener(name, handler) { this.events[name] = handler; }
  }
  const nodes = new Map();
  const document = {
    addEventListener() {},
    createElement: () => new Element(),
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
    querySelectorAll: (selector) => selector === '#workload-tray wa-button[data-devices]'
      ? nodes.get('workload-tray').children.flatMap((group) => group.children.slice(1)).flatMap((card) => card.children[0].children[2].children)
      : []
  };
  const context = vm.createContext({ document, matchMedia: (query) => ({ matches: coarse && query === '(pointer: coarse)' }) });
  vm.runInContext(source, context);
  vm.runInContext(`
    catalogue = {
      models: [{ id: 'single-text', label: 'Single Text' }, { id: 'dual-only', label: 'Dual Only' }],
      rules: {
        workloads: { 'single-text': { kind: 'text', runtime: 'text' }, 'dual-only': { kind: 'text', runtime: 'text' }, image: { kind: 'image', runtime: 'render-pool', label: 'Image' } },
        profiles: { 'single-text': ['0', '1', '0,1'], 'dual-only': ['0,1'], image: ['0', '1'] }
      }
    };
    draft = ${JSON.stringify(allocation)};
    let previews = 0;
    preview = () => { previews++; };
    renderTray(); renderBoard();
  `, context);
  return {
    cards: () => nodes.get('gpu-board').children,
    tray: () => nodes.get('workload-tray').children.flatMap((group) => group.children.slice(1)),
    select: (device) => nodes.get('gpu-board').children.find((card) => card.dataset.device === device).children[0].children[1],
    buttons: () => nodes.get('workload-tray').children.flatMap((group) => group.children.slice(1)).flatMap((card) => card.children[0].children[2].children),
    node: (id) => nodes.get(id),
    draft: () => JSON.parse(vm.runInContext('JSON.stringify(draft)', context)),
    previews: () => vm.runInContext('previews', context)
  };
}

test('both single-GPU dropdowns expose single and dual variants with explicit labels', () => {
  const ui = board([]);
  for (const device of ['0', '1']) {
    const options = ui.select(device).children;
    assert.equal(options.find((option) => option.value === 'single-text:' + device).textContent, 'Single Text · GPU ' + Number(device));
    assert.equal(options.find((option) => option.value === 'single-text:0,1').textContent, 'Single Text · Both GPUs');
    assert.equal(options.some((option) => option.value === 'single-text:' + (device === '0' ? '1' : '0')), false);
    assert.equal(options.some((option) => option.value === 'dual-only:' + device), false);
    assert.equal(options.some((option) => option.value === 'dual-only:0,1'), true);
  }
});

test('dropdown preserves current placement and expands either single GPU to both', () => {
  for (const device of ['0', '1']) {
    const ui = board([{ profile: 'single-text', devices: [device] }, { profile: 'image', devices: [device === '0' ? '1' : '0'] }]);
    const select = ui.select(device);
    assert.equal(select.value, 'single-text:' + device);
    select.value = 'single-text:0,1'; select.events.change();
    assert.deepEqual(ui.draft(), [{ profile: 'single-text', devices: ['0', '1'] }]);
    assert.equal(ui.cards().length, 1);
    assert.equal(ui.select('0').value, 'single-text:0,1');
    assert.equal(ui.previews(), 1);
  }
});

test('dual dropdown can switch to either single GPU and restores the other empty card', () => {
  for (const device of ['0', '1']) {
    const ui = board([{ profile: 'single-text', devices: ['0', '1'] }]);
    const select = ui.select('0');
    assert.equal(select.children.some((option) => option.value === 'single-text:' + device), true);
    select.value = 'single-text:' + device; select.events.change();
    assert.deepEqual(ui.draft(), [{ profile: 'single-text', devices: [device] }]);
    assert.equal(ui.cards().length, 2);
    assert.equal(ui.select(device === '0' ? '1' : '0').value, 'off');
  }
});

test('occupied single GPU buttons are disabled while Both GPUs replaces the preview', () => {
  for (const allocation of [
    [{ profile: 'single-text', devices: ['0'] }, { profile: 'image', devices: ['1'] }],
    [{ profile: 'single-text', devices: ['0', '1'] }]
  ]) {
    const ui = board(allocation);
    for (const button of ui.buttons()) {
      assert.equal(button.disabled, button.dataset.devices !== '0,1');
    }
    const both = ui.buttons().find((button) => button.textContent === 'Both GPUs');
    both.events.click();
    assert.deepEqual(ui.draft(), [{ profile: 'dual-only', devices: ['0', '1'] }]);
    assert.equal(ui.previews(), 1);
  }
});

test('single GPU buttons follow preview occupancy while Both GPUs stays enabled', () => {
  const ui = board([{ profile: 'single-text', devices: ['0', '1'] }]);
  const select = ui.select('0');
  select.value = 'single-text:1'; select.events.change();
  for (const button of ui.buttons()) {
    assert.equal(button.disabled, button.dataset.devices === '1');
  }
  const empty = ui.select('1');
  empty.value = 'off'; empty.events.change();
  assert.equal(ui.buttons().every((button) => !button.disabled), true);
});

test('touch screens keep native placement buttons instead of drag and drop', () => {
  const mouse = board([{ profile: 'single-text', devices: ['0'] }]);
  assert.equal(mouse.tray().every((card) => card.draggable), true);
  assert.equal(mouse.cards()[0].draggable, true);
  assert.equal(mouse.buttons().some((button) => button.variant), false);
  const touch = board([{ profile: 'single-text', devices: ['0'] }], { coarse: true });
  assert.equal(touch.tray().some((card) => card.draggable), false);
  assert.equal(touch.cards().some((card) => card.draggable), false);
  assert.equal(touch.buttons().every((button) => !button.variant), true);
  const image = touch.tray().find((card) => card.children[0].children[0].textContent === 'Image');
  image.children[0].children[2].children.find((button) => button.textContent === 'GPU 1').events.click();
  assert.deepEqual(touch.draft().map((item) => item.devices), [['0'], ['1']]);
});

test('workload controls have visible labels and empty previews cannot be cleared again', () => {
  const ui = board([]);
  assert.equal(ui.select('0').label, 'Workload');
  assert.equal(ui.select('1').label, 'Workload');
  assert.equal(ui.node('clear-board').disabled, true);
  ui.select('0').value = 'single-text:0';
  ui.select('0').events.change();
  assert.equal(ui.node('clear-board').disabled, false);
  assert.equal(ui.node('board-state').textContent, 'Unapplied Changes');
  assert.equal(ui.node('board-state').variant, 'warning');
});
