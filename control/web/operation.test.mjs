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
    render(id, output, open = false) {
      context.job = { id, output, state: 'running', action: 'check', started_at: Date.now() / 1000 };
      vm.runInContext(`renderJob(job, ${open})`, context);
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
