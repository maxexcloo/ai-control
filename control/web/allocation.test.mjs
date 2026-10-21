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
    replaceChildren(...children) { this.children = children; if (this === nodes.get('gpu-board')) document.activeElement = document.body; }
    setAttribute() {}
    closest(selector) { return selector === '#gpu-board wa-select' && this.id?.startsWith('gpu-select-') ? this : null; }
    focus() { document.activeElement = this; }
    addEventListener(name, handler) { this.events[name] = handler; }
  }
  const nodes = new Map();
  const document = {
    addEventListener() {},
    body: {},
    createElement: () => new Element(),
    getElementById: (id) => {
      if (id.startsWith('gpu-select-')) return nodes.get('gpu-board')?.children.flatMap((card) => card.children[0].children).find((node) => node.id === id) || null;
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
    querySelectorAll: (selector) => selector === '#workload-tray wa-button[data-devices]'
      ? nodes.get('workload-tray').children.flatMap((group) => group.children.slice(1)).flatMap((card) => card.children[0].children[2].children)
      : selector === '#workload-tray wa-badge[data-profile]'
        ? nodes.get('workload-tray').children.flatMap((group) => group.children.slice(1)).map((card) => card.children[0].children[3])
        : []
  };
  const context = vm.createContext({ document, requestAnimationFrame: (callback) => callback(), matchMedia: (query) => ({ matches: coarse && query === '(pointer: coarse)' }) });
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
    focus: (device) => { document.activeElement = document.getElementById('gpu-select-' + device); },
    active: () => document.activeElement,
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

test('occupied single and dual GPU destinations both replace only the preview', () => {
  const ui = board([{ profile: 'single-text', devices: ['0'] }, { profile: 'image', devices: ['1'] }]);
  assert.equal(ui.buttons().every((button) => !button.disabled), true);
  const image = ui.tray().find((card) => card.children[0].children[0].textContent === 'Image');
  const replace = image.children[0].children[2].children.find((button) => button.dataset.devices === '0');
  assert.match(replace.title, /Replace in the preview/);
  replace.events.click();
  assert.deepEqual(ui.draft(), [{ profile: 'image', devices: ['0'] }]);
  assert.equal(ui.previews(), 1);
  const both = ui.buttons().find((button) => button.textContent === 'Both GPUs');
  both.events.click();
  assert.deepEqual(ui.draft(), [{ profile: 'dual-only', devices: ['0', '1'] }]);
  assert.equal(ui.previews(), 2);
});

test('placement hints distinguish replacing occupied GPUs from placing on idle GPUs', () => {
  const ui = board([{ profile: 'single-text', devices: ['1'] }]);
  for (const button of ui.buttons()) {
    assert.equal(button.disabled, false);
    assert.equal(button.title.includes('Replace'), button.dataset.devices.includes('1'));
  }
  const empty = ui.select('1');
  empty.value = 'off'; empty.events.change();
  assert.equal(ui.buttons().every((button) => !button.disabled && button.title === 'Place in the preview'), true);
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

test('expanding GPU 1 to both GPUs retains keyboard focus on the shared selector', () => {
  const ui = board([{ profile: 'single-text', devices: ['1'] }]);
  ui.focus('1');
  const select = ui.select('1'); select.value = 'single-text:0,1'; select.events.change();
  assert.equal(ui.active(), ui.select('0'));
});


test('placement feedback follows the preview without applying runtime changes', () => {
  const ui = board([{ profile: 'single-text', devices: ['1'] }]);
  const selected = () => ui.tray().map((card) => card.children[0].children[3]).filter((badge) => !badge.hidden);
  assert.deepEqual(selected().map((badge) => badge.textContent), ['Preview: GPU 1']);
  const placement = ui.buttons().find((button) => button.dataset.profile === 'single-text' && button.dataset.devices === '0,1');
  assert.equal(placement.children[0].textContent, ' for Single Text in the preview');
  assert.equal(placement.children[0].className, 'wa-visually-hidden');
  placement.events.click();
  assert.deepEqual(selected().map((badge) => badge.textContent), ['Preview: Both GPUs']);
  assert.equal(ui.previews(), 1);
  ui.select('0').value = 'off'; ui.select('0').events.change();
  assert.equal(selected().length, 0);
  assert.equal(ui.previews(), 2);
});
