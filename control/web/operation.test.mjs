import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8').split("$('login-form').addEventListener")[0];
function modal() {
  const nodes = new Map();
  const frames = [];
  const document = {
    addEventListener() {},
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {
        events: {}, scrollTop: 0, scrollHeight: 500, clientHeight: 200,
        addEventListener(name, handler) { this.events[name] = handler; }
      });
      return nodes.get(id);
    }
  };
  const context = vm.createContext({ document, requestAnimationFrame: (callback) => frames.push(callback) });
  vm.runInContext(source, context);
  return {
    output: document.getElementById('job-output'),
    dialog: document.getElementById('operation'),
    status: document.getElementById('operation-status'),
    message: document.getElementById('operation-message'),
    view: document.getElementById('view-operation'),
    render(id, output, open = false, { historical = false, state = 'running' } = {}) {
      context.job = { id, output, state, action: 'check', started_at: Date.now() / 1000 };
      vm.runInContext(`renderJob(job, ${open}, ${historical})`, context);
    },
    paint() { frames.splice(0).forEach((callback) => callback()); }
  };
}

test('new output follows the tail after layout updates', () => {
  const ui = modal();
  ui.render('first', 'Starting'); ui.paint();
  ui.output.scrollTop = 300;
  ui.render('first', 'Starting\nMore output');
  ui.output.scrollHeight = 800;
  ui.paint();
  assert.equal(ui.output.scrollTop, 800);
});

test('scrolling up preserves the reading position until the user returns to the tail', () => {
  const ui = modal();
  ui.render('first', 'Starting'); ui.paint();
  ui.output.scrollTop = 40;
  ui.render('first', 'More output'); ui.paint();
  assert.equal(ui.output.scrollTop, 40);
  ui.output.scrollTop = 300;
  ui.render('first', 'Even more output'); ui.paint();
  assert.equal(ui.output.scrollTop, 500);
});

test('opening another job starts at its tail and ignores stale pending frames', () => {
  const ui = modal();
  ui.render('first', 'Old output');
  ui.output.scrollTop = 40;
  ui.render('second', 'New output', true);
  ui.paint();
  assert.equal(ui.output.scrollTop, 500);
  ui.output.scrollTop = 0;
  ui.dialog.events['wa-after-show']();
  assert.equal(ui.output.scrollTop, 500);
});

test('background operations provide status without taking focus in a modal', () => {
  const ui = modal();
  ui.render('background', 'Checking');
  assert.equal(Boolean(ui.dialog.open), false);
  assert.equal(ui.status.hidden, false);
  assert.equal(ui.message.textContent, 'Check Configuration · Running');
  ui.view.events.click();
  assert.equal(ui.dialog.open, true);
});

test('an explicitly opened operation remains dismissible while polling continues', () => {
  const ui = modal();
  ui.render('apply', 'Starting', true);
  assert.equal(ui.dialog.open, true);
  ui.dialog.open = false;
  ui.render('apply', 'Still starting');
  assert.equal(ui.dialog.open, false);
  assert.equal(ui.status.hidden, false);
});

test('historical output remains selected while an active operation updates', () => {
  const ui = modal();
  ui.render('active', 'Active output');
  ui.render('old', 'Old failure details', true, { historical: true, state: 'failed' });
  ui.render('active', 'New active output'); ui.paint();
  assert.equal(ui.output.textContent, 'Old failure details');
  assert.equal(ui.message.textContent, 'Check Configuration · Running');
  ui.view.events.click();
  assert.equal(ui.output.textContent, 'New active output');
});

test('operation feedback uses native success and danger variants for terminal states', () => {
  const ui = modal();
  ui.render('first', 'Complete', false, { state: 'completed' });
  assert.equal(ui.status.variant, 'success');
  ui.render('second', 'Failure', false, { state: 'failed' });
  assert.equal(ui.status.variant, 'danger');
});
