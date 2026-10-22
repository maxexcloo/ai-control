import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

// Exercise the real restart handlers without polling or network requests.
const source = readFileSync(new URL("./app.js", import.meta.url), "utf8").split(
  '$("login-form").addEventListener',
)[0];
function restartUI() {
  class Element {
    children = [];
    dataset = {};
    events = {};
    disabled = false;
    append(...nodes) {
      this.children.push(...nodes);
    }
    addEventListener(name, handler) {
      this.events[name] = handler;
    }
  }
  const nodes = new Map();
  const document = {
    addEventListener() {},
    createElement: () => new Element(),
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
  };
  const context = vm.createContext({ document, requestAnimationFrame() {} });
  vm.runInContext(source, context);
  vm.runInContext(
    `
    catalogue = { rules: { workloads: { 'dual-text': { kind: 'text', label: 'Text', model_label: 'Dual Text · 2 GPUs', runtime: 'text' } } } };
    var requests = [];
    api = async (path, payload) => { requests.push([path, payload]); return { id: 'job', state: 'running', restart: payload, started_at: 0, output: '' }; };
    refresh = async () => {};
  `,
    context,
  );
  return {
    context,
    node: (id) => nodes.get(id),
    requests: () => JSON.parse(vm.runInContext("JSON.stringify(requests)", context)),
  };
}

test("restart confirms and submits the live profile and GPUs unchanged", async () => {
  const ui = restartUI();
  ui.context.item = {
    runtime: "text",
    profile: "dual-text",
    devices: ["0", "1"],
    health: "unhealthy",
    running: true,
  };
  const button = vm.runInContext("restartButton(item, false)", ui.context);
  assert.equal(button.textContent, "Restart");
  assert.equal(button.disabled, false);
  button.events.click();
  assert.equal(ui.node("restart-dialog").open, true);
  assert.equal(ui.node("restart-summary").textContent, "Restart Dual Text on GPU 0 & GPU 1?");
  assert.deepEqual(ui.requests(), []);
  await ui.node("restart-confirm").events.click();
  assert.equal(ui.node("restart-dialog").open, false);
  assert.deepEqual(ui.requests(), [
    ["restart", { runtime: "text", profile: "dual-text", devices: ["0", "1"] }],
  ]);
  assert.equal(
    vm.runInContext('jobTitle({ restart: { profile: "dual-text" } })', ui.context),
    "Restarting Dual Text",
  );
  await ui.node("restart-confirm").events.click();
  assert.equal(ui.requests().length, 1);
});

test("restart is unavailable while another operation runs", () => {
  const ui = restartUI();
  ui.context.item = { runtime: "text", profile: "dual-text", devices: ["0"] };
  assert.equal(vm.runInContext("restartButton(item, true)", ui.context).disabled, true);
});
