let draggedBoardProfile;
let catalogue, live, draft = [], initialised = false, jobId, lastJob, plan, planVersion = 0, refreshing = false, loading = false, historySignature, viewedJobId, viewedJob;
const $ = (id) => document.getElementById(id);
const gpuLabel = (device) => 'GPU ' + Number(device);
const runtime = (profile) => catalogue.rules.workloads[profile].runtime;
const runtimeName = (name) => catalogue.rules.runtime_names[name];
const showError = (message) => { $('error').textContent = message; $('error').hidden = !message; };
function showConnection(message = '', busy = false) {
  $('connection-state').hidden = !message;
  $('connection-state').variant = busy ? 'neutral' : 'warning';
  $('connection-message').textContent = message;
  $('connection-progress').hidden = !busy;
  $('retry-connection').hidden = busy || !message;
}

// Touch screens place workloads with the GPU buttons and dropdowns; drag and drop stays for mice.
const touch = () => Boolean(globalThis.matchMedia?.('(pointer: coarse)').matches);
const labels = () => Object.fromEntries(workloads().map((item) => [item.profile, item.label]));
function workloads() {
  return Object.entries(catalogue.rules.workloads).map(([profile, item]) => ({
    profile, label: item.kind === 'text' ? catalogue.models.find((model) => model.id === profile).label.replace(' · 2 GPUs', '') : item.label,
    group: item.kind === 'text' ? 'Text' : 'Media'
  })).sort((a, b) => a.label.localeCompare(b.label));
}
function liveAllocation() {
  return Object.values(live?.runtimes || {}).filter((item) => item.running).map((item) => ({ profile: item.profile, devices: [...item.devices] }));
}
function canonical(items) { return JSON.stringify([...items].sort((a, b) => a.devices.join().localeCompare(b.devices.join()))); }
function showSession(signedIn) {
  if (!signedIn) { $('operation').open = false; $('recent-operations').open = false; $('header-menu').open = false; }
  $('dashboard').hidden = !signedIn; $('login').hidden = signedIn;
  for (const id of ['logout', 'logout-menu', 'recent-operations']) $(id).hidden = !signedIn;
}
async function api(path, payload) {
  const response = await fetch('/api/' + path, { method: payload === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-AI-Control': '1' }, body: payload === undefined ? undefined : JSON.stringify(payload) });
  if (response.status === 401) { showSession(false); showConnection(); }
  const text = await response.text();
  let data; try { data = JSON.parse(text); } catch { data = { error: text }; }
  if (!response.ok) { const error = new Error(data.error || 'The control request failed.'); error.status = response.status; throw error; }
  return data;
}
function element(tag, text, className) {
  const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node;
}
function cardContent(card) {
  const content = element('div', undefined, 'wa-stack wa-gap-s');
  card.append(content);
  return content;
}
function button(label, action) { const node = element('wa-button', label); node.type = 'button'; node.addEventListener('click', action); return node; }
function place(profile, placement) {
  const allowed = catalogue.rules.profiles[profile];
  const devices = (allowed.length === 1 ? allowed[0] : placement).split(',');
  if (!allowed.includes(devices.join(','))) { showError('This workload needs a different GPU placement.'); return; }
  draft = draft.filter((item) => runtime(item.profile) !== runtime(profile) && !item.devices.some((d) => devices.includes(d)));
  draft.push({ profile, devices }); renderBoard(); preview();
}
function temperature(value) { return Number.isFinite(value) ? Math.round(value) + ' °C' : ''; }
function hardware(device) {
  const metrics = live?.gpu_metrics?.[device];
  return [metrics?.name, metrics?.vram_total_bytes > 0 ? Math.round(metrics.vram_total_bytes / 2 ** 30) + ' GiB' : ''].filter(Boolean).join(' · ');
}
function draggable(node, profile) {
  if (touch()) return;
  node.draggable = true; node.dataset.profile = profile;
  node.addEventListener('dragend', () => { draggedBoardProfile = null; });
  node.addEventListener('dragstart', (event) => { draggedBoardProfile = node.classList.contains('gpu-board-card') ? profile : null; event.dataTransfer.setData('text/plain', profile); event.dataTransfer.effectAllowed = 'move'; });
}
document.addEventListener('dragover', (event) => {
  if (draggedBoardProfile && !event.target.closest('#gpu-board')) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; }
});
document.addEventListener('drop', (event) => {
  if (!draggedBoardProfile || event.target.closest('#gpu-board')) return;
  event.preventDefault();
  draft = draft.filter((item) => item.profile !== draggedBoardProfile);
  draggedBoardProfile = null; renderBoard(); preview();
});
function dropzone(node, device) {
  node.addEventListener('dragover', (event) => { event.preventDefault(); node.classList.add('drop-target'); event.dataTransfer.dropEffect = 'move'; });
  node.addEventListener('dragleave', () => node.classList.remove('drop-target'));
  node.addEventListener('drop', (event) => { event.preventDefault(); node.classList.remove('drop-target'); const profile = event.dataTransfer.getData('text/plain'); if (labels()[profile]) place(profile, device); });
}
function renderBoard() {
  const focusedSelect = document.activeElement?.closest?.('#gpu-board wa-select')?.id;
  const board = $('gpu-board'); board.replaceChildren();
  for (const device of ['0', '1']) {
    const item = draft.find((value) => value.devices.includes(device));
    const shared = item?.devices.length === 2;
    if (shared && device !== item.devices[0]) continue;
    const card = element('wa-card', undefined, 'workload-card gpu-board-card' + (item ? ' allocated' : '') + (shared ? ' dual' : ''));
    const content = cardContent(card);
    card.dataset.device = device; card.setAttribute('aria-label', (shared ? 'Both GPUs' : gpuLabel(device)) + ' Preview & Drop Zone');
    const heading = element('div', undefined, 'gpu-status-heading wa-split wa-flex-wrap wa-gap-s'); const title = element('h3', shared ? 'GPU 0 & GPU 1' : gpuLabel(device), 'wa-heading-m'); heading.append(title);
    const descriptions = [...new Set((shared ? item.devices : [device]).map(hardware).filter(Boolean))];
    const hardwareName = descriptions.length ? element('span', descriptions.join(' / '), 'wa-font-size-s wa-color-text-quiet hardware-name board-hardware') : null;
    const select = element('wa-select'); select.id = 'gpu-select-' + device; select.label = 'Workload'; select.setAttribute('aria-label', (shared ? 'Both GPUs' : gpuLabel(device)) + ' Workload');
    const empty = element('wa-option', 'Empty'); empty.value = 'off'; select.append(empty);
    for (const workload of workloads()) {
      const allowed = catalogue.rules.profiles[workload.profile];
      for (const placement of allowed.filter((value) => shared || value === device || value === '0,1')) {
        const option = element('wa-option', workload.label + ' · ' + (placement === '0,1' ? 'Both GPUs' : gpuLabel(placement)));
        option.value = workload.profile + ':' + placement; select.append(option);
      }
    }
    select.value = item ? item.profile + ':' + item.devices.join(',') : 'off';
    select.addEventListener('change', () => {
      if (select.value === 'off') { draft = draft.filter((value) => !value.devices.includes(device)); renderBoard(); preview(); }
      else place(...select.value.split(':'));
    });
    const current = (number) => {
      const owner = live && liveAllocation().find((value) => value.devices.includes(number));
      return owner ? labels()[owner.profile] || owner.profile : 'Idle';
    };
    const liveCaption = shared ? item.devices.map((number) => gpuLabel(number) + ': ' + current(number)).join(' · ') : current(device);
    content.append(heading);
    if (hardwareName) content.append(hardwareName);
    content.append(select, element('span', 'Live: ' + liveCaption, 'wa-font-size-s wa-color-text-quiet live-allocation'));

    const devices = shared ? item.devices : [device];
    for (const number of devices) { const thermal = element('span', undefined, 'wa-font-size-s wa-color-text-quiet gpu-hardware'); thermal.dataset.device = number; content.append(thermal); }
    dropzone(card, device); if (item) draggable(card, item.profile); board.append(card);
  }
  renderBoardTemperatures(); updatePlacementButtons();
  if (focusedSelect) requestAnimationFrame(() => {
    if (document.activeElement === document.body) ($(focusedSelect) || $('gpu-select-0'))?.focus();
  });
  const unchanged = live && canonical(draft) === canonical(liveAllocation());
  $('board-state').textContent = unchanged ? 'Matches Live' : 'Unapplied Changes';
  $('board-state').variant = unchanged ? 'neutral' : 'warning';
  $('reset-board').disabled = Boolean(unchanged);
  $('clear-board').disabled = !draft.length;
}
function renderBoardTemperatures() {
  for (const node of document.querySelectorAll('#gpu-board .gpu-hardware')) {
    const value = temperature(live?.gpu_metrics?.[node.dataset.device]?.temperature_celsius);
    node.hidden = !value;
    node.textContent = (node.closest('.dual') ? gpuLabel(node.dataset.device) + ' · ' : '') + value;
  }
}
function renderTray() {
  $('workload-tray').replaceChildren();
  for (const group of ['Text', 'Media']) {
    const container = element('div', undefined, 'workload-group wa-stack wa-gap-s'); container.append(element('h4', group, 'wa-heading-m'));
    for (const item of workloads().filter((value) => value.group === group)) {
      const card = element('wa-card', undefined, 'workload-card');
      const content = cardContent(card);
      const allowed = catalogue.rules.profiles[item.profile];
      const title = element('h5', item.label, 'wa-heading-m');
      content.append(title, element('span', allowed.length === 1 ? (allowed[0] === '0,1' ? 'Requires Both GPUs' : gpuLabel(allowed[0]) + ' Only') : (allowed.includes('0,1') ? 'One Or Both GPUs' : 'One GPU'), 'wa-font-size-s wa-color-text-quiet'));
      const controls = element('div', undefined, 'card-actions wa-cluster wa-gap-xs');
      for (const placement of allowed) {
        const choice = button(placement === '0,1' ? 'Both GPUs' : gpuLabel(placement), () => place(item.profile, placement));
        choice.size = 'm';
        choice.dataset.devices = placement;
        choice.title = 'Place in the preview';
        controls.append(choice);
      }
      content.append(controls); draggable(card, item.profile); container.append(card);
    }
    $('workload-tray').append(container);
  }
  updatePlacementButtons();
}
function updatePlacementButtons() {
  const used = new Set(draft.flatMap((item) => item.devices));
  for (const choice of document.querySelectorAll('#workload-tray wa-button[data-devices]')) {
    choice.disabled = false;
    choice.title = choice.dataset.devices.split(',').some((device) => used.has(device)) ? 'Replace in the preview; Apply is required to change running workloads.' : 'Place in the preview';
  }
}
async function preview() {
  const version = ++planVersion; plan = null; $('apply-board').disabled = true; $('change-preview').textContent = 'Checking the allocation…';
  try {
    const result = await api('board/plan', { allocation: draft });
    if (version !== planVersion) return;
    plan = result; const container = $('change-preview'); container.replaceChildren();
    if (!result.stop.length && !result.start.length) container.append(element('p', 'No changes needed. Running workloads stay as they are.'));
    else {
      if (result.stop.length) container.append(element('p', 'Will stop: ' + result.stop.map((name) => name === 'text' ? live.text_model : runtimeName(name)).join('; ') + '.'));
      if (result.start.length) container.append(element('p', 'Will start: ' + result.start.map((item) => (labels()[item.profile] || item.profile) + ' on ' + item.devices.map(gpuLabel).join(', ')).join('; ') + '.'));
      if (result.keep.length) container.append(element('p', 'Keeps running: ' + result.keep.map((name) => runtimeName(name)).join(', ') + '.'));
    }
    $('apply-board').disabled = Boolean(live?.job) || (!result.stop.length && !result.start.length);
  } catch (error) {
    if (version === planVersion) {
      const failure = element('wa-callout', 'Could not preview changes. ' + error.message);
      failure.variant = 'danger'; failure.setAttribute('role', 'alert');
      $('change-preview').replaceChildren(failure);
    }
  }
}
function metricBar(label, value, maximum, text) {
  const metric = element('div', undefined, 'gpu-metric wa-stack wa-gap-xs');
  const heading = element('div', undefined, 'metric-heading wa-font-size-s wa-split wa-flex-wrap wa-gap-xs');
  heading.append(element('span', label), element('strong', text));
  const track = element('wa-progress-bar');
  track.label = label + ': ' + text;
  track.setAttribute('aria-label', label);
  track.setAttribute('aria-valuetext', text);
  track.value = Number.isFinite(value) && Number.isFinite(maximum) && maximum > 0 ? Math.min(100, Math.max(0, value / maximum * 100)) : 0;
  metric.append(heading, track); return metric;
}
let previousHost;
function byteSize(value) {
  if (!Number.isFinite(value)) return 'Unavailable';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log2(Math.max(1, value)) / 10));
  return (value / 1024 ** index).toFixed(index ? 1 : 0) + ' ' + units[index];
}
function renderHost(current) {
  if (Number.isFinite(current?.sampled_at_seconds) && previousHost && current.sampled_at_seconds === previousHost.sampled_at_seconds) return;
  const container = $('host-status'); container.replaceChildren();
  const elapsed = current?.sampled_at_seconds - previousHost?.sampled_at_seconds;
  function rate(group, field) {
    const next = current?.[group], before = previousHost?.[group];
    const unchanged = group === 'disk' ? next?.device && next.device === before?.device : next?.interface && next.interface === before?.interface;
    const delta = next?.[field] - before?.[field];
    return unchanged && elapsed > 0 && Number.isFinite(delta) && delta >= 0 ? byteSize(delta / elapsed) + '/s' : Number.isFinite(next?.[field]) ? 'Sampling…' : 'Unavailable';
  }
  function card(title, hint) {
    const node = element('wa-card', undefined, 'host-card'); node.dataset.kind = title.toLowerCase();
    const content = cardContent(node);
    const heading = element('div', undefined, 'metric-heading wa-font-size-s wa-split wa-flex-wrap wa-gap-xs'); const titleNode = element('h3', title, 'wa-heading-m'); heading.append(titleNode, element('span', hint, 'hardware-name')); content.append(heading); container.append(node); return content;
  }
  function transfers(node, first, second) {
    const row = element('div', undefined, 'transfer-rates wa-font-size-s wa-grid wa-gap-s');
    for (const [label, value] of [first, second]) { const item = element('div', undefined, 'wa-stack wa-gap-xs'); item.append(element('span', label, 'wa-font-size-s wa-color-text-quiet'), element('strong', value)); row.append(item); }
    node.append(row);
  }
  const total = current?.cpu?.total_ticks - previousHost?.cpu?.total_ticks;
  const idle = current?.cpu?.idle_ticks - previousHost?.cpu?.idle_ticks;
  const busy = elapsed > 0 && total > 0 && idle >= 0 ? Math.min(100, Math.max(0, (total - idle) / total * 100)) : undefined;
  const cpu = card('CPU', current?.cpu?.name || '');
  cpu.append(metricBar('Activity', busy, 100, Number.isFinite(busy) ? busy.toFixed(1) + '%' : Number.isFinite(current?.cpu?.total_ticks) ? 'Sampling…' : 'Unavailable'));
  const cpuDetail = [current?.cpu?.cores ? current.cpu.cores + ' Threads' : '', temperature(current?.cpu?.temperature_celsius)].filter(Boolean).join(' · ');
  if (cpuDetail) cpu.append(element('span', cpuDetail, 'wa-font-size-s wa-color-text-quiet'));
  const memory = current?.memory;
  const ram = card('Memory', 'RAM');
  ram.append(metricBar('Used', memory?.used_bytes, memory?.total_bytes, memory?.total_bytes > 0 ? byteSize(memory.used_bytes) + ' / ' + byteSize(memory.total_bytes) : 'Unavailable'));
  if (Number.isFinite(memory?.available_bytes)) ram.append(element('span', byteSize(memory.available_bytes) + ' Available', 'wa-font-size-s wa-color-text-quiet disk-free'));
  const disk = current?.disk;
  const storage = card('Disk', disk?.path || 'AI Data');
  storage.append(metricBar('Used', disk?.used_bytes, disk?.total_bytes, disk?.total_bytes > 0 ? byteSize(disk.used_bytes) + ' / ' + byteSize(disk.total_bytes) : 'Unavailable'));
  if (Number.isFinite(disk?.free_bytes)) storage.append(element('span', byteSize(disk.free_bytes) + ' Free', 'wa-font-size-s wa-color-text-quiet disk-free'));
  transfers(storage, ['Read', rate('disk', 'read_bytes')], ['Write', rate('disk', 'write_bytes')]);
  transfers(card('Network', current?.network?.interface || ''), ['Receive', rate('network', 'received_bytes')], ['Send', rate('network', 'sent_bytes')]);
  previousHost = current;
}
function renderStatus(data) {
  const focusedButton = document.activeElement?.closest?.('#live-gpus wa-button[data-runtime]');
  const focusedRestart = focusedButton && { runtime: focusedButton.dataset.runtime, device: focusedButton.closest('.gpu-status').dataset.device };
  const followsLive = !live || canonical(draft) === canonical(liveAllocation());
  const changed = live && canonical(Object.values(live?.runtimes || {}).filter((item) => item.running).map((item) => ({ profile: item.profile, devices: item.devices }))) !== canonical(Object.values(data.runtimes).filter((item) => item.running).map((item) => ({ profile: item.profile, devices: item.devices })));
  live = data; renderHost(data.host_metrics); $('live-gpus').replaceChildren();
  for (const device of ['0', '1']) {
    const metrics = data.gpu_metrics?.[device];
    const column = element('wa-card', undefined, 'gpu-status'); column.dataset.device = device;
    const content = cardContent(column);
    const heading = element('div', undefined, 'gpu-status-heading wa-split wa-flex-wrap wa-gap-s');
    const title = element('div', undefined, 'wa-cluster wa-gap-s'); title.append(element('h3', gpuLabel(device), 'wa-heading-m')); heading.append(title); content.append(heading);
    const owners = Object.entries(data.runtimes).filter(([, item]) => item.running && item.devices.includes(device)).map(([name, item]) => ({ ...item, runtime: name }));
    const health = owners.length ? (owners.every((item) => item.health === 'healthy') ? 'Ready' : owners.find((item) => item.health !== 'healthy').health) : 'Idle';
    const badge = element('wa-badge', ({ starting: 'Starting', unhealthy: 'Unhealthy', unknown: 'Unknown' }[health] || health), 'health-badge'); badge.variant = health === 'Ready' ? 'success' : health === 'unhealthy' ? 'danger' : ['starting', 'unknown'].includes(health) ? 'warning' : 'neutral'; title.append(badge);
    for (const item of owners) heading.append(restartButton(item, Boolean(data.job)));
    if (metrics?.name) content.append(element('span', metrics.name, 'wa-font-size-s wa-color-text-quiet hardware-name'));
    if (!owners.length) content.append(element('strong', data['gpu' + device], 'gpu-workload'));
    for (const item of owners) {
      content.append(element('strong', labels()[item.profile] || item.profile, 'gpu-workload'));
    }
    const memory = metrics?.vram_total_bytes > 0 && Number.isFinite(metrics?.vram_used_bytes);
    content.append(metricBar('Activity', metrics?.busy_percent, 100, Number.isFinite(metrics?.busy_percent) ? metrics.busy_percent + '%' : 'Unavailable'));
    content.append(metricBar('VRAM', memory ? metrics.vram_used_bytes : undefined, metrics?.vram_total_bytes, memory ? (metrics.vram_used_bytes / 2 ** 30).toFixed(1) + ' / ' + (metrics.vram_total_bytes / 2 ** 30).toFixed(1) + ' GiB' : 'Unavailable'));
    const thermal = [temperature(metrics?.temperature_celsius), Number.isFinite(metrics?.hotspot_celsius) ? 'Hotspot ' + temperature(metrics.hotspot_celsius) : ''].filter(Boolean).join(' · ');
    if (thermal) content.append(element('span', thermal, 'wa-font-size-s wa-color-text-quiet gpu-temperature'));
    $('live-gpus').append(column);
  }
  if (focusedRestart) requestAnimationFrame(() => {
    const button = [...document.querySelectorAll('#live-gpus wa-button[data-runtime]')].find((node) => node.dataset.runtime === focusedRestart.runtime && node.closest('.gpu-status').dataset.device === focusedRestart.device && !node.disabled);
    if (document.activeElement === document.body) button?.focus();
  });
  renderBoardTemperatures();
  $('updated').dataset.state = 'live';
  $('updated').textContent = 'Live · ' + new Date(data.updated).toLocaleTimeString();
  if (data.job) { jobId = data.job.id; $('apply-board').disabled = true; }
  if (!initialised) { initialised = true; draft = liveAllocation(); renderBoard(); preview(); }
  else if (changed) { if (followsLive) draft = liveAllocation(); renderBoard(); preview(); }
  else if (!data.job && plan) $('apply-board').disabled = !plan.stop.length && !plan.start.length;
}
function renderJob(job, open = false, historical = false) {
  const state = ({ queued: 'Queued', running: 'Running', completed: 'Completed', failed: 'Failed' }[job.state] || 'Needs Attention');
  if (!historical) {
    lastJob = job;
    $('operation-status').hidden = false;
    $('operation-status').variant = ({ completed: 'success', failed: 'danger', queued: 'neutral', running: 'neutral' }[job.state] || 'warning');
    $('operation-message').textContent = jobTitle(job) + ' · ' + state + (job.stage ? ' · ' + job.stage : '');
  }
  if (open) viewedJobId = job.id;
  // Keep a historical transcript open while a different active operation is polled.
  if (viewedJobId && viewedJobId !== job.id) return;
  const changed = viewedJob?.id !== job.id;
  viewedJob = job;
  if (open && !$('operation').open) $('operation').open = true;
  $('operation').label = jobTitle(job);
  const end = job.finished_at || Date.now() / 1000;
  const seconds = Math.max(0, Math.floor(end - job.started_at));
  const age = Math.max(0, Math.floor(Date.now() / 1000 - (job.updated_at || job.started_at)));
  const active = job.state === 'running' || job.state === 'queued';
  $('job-time').textContent = Math.floor(seconds / 60) + 'm ' + seconds % 60 + 's · ' + (active ? state + ' · Last Output ' + age + 's Ago' : state);
  const output = $('job-output'); const follow = changed || open || output.scrollHeight - output.scrollTop - output.clientHeight < 40;
  const text = job.output || 'Waiting for output…';
  if (output.textContent !== text) output.textContent = text;
  if (follow) requestAnimationFrame(() => { if (viewedJob?.id === job.id) output.scrollTop = output.scrollHeight; });
}
$('view-operation').addEventListener('click', () => { if (lastJob) renderJob(lastJob, true); });
$('operation').addEventListener('wa-after-hide', () => { viewedJobId = undefined; });
$('operation').addEventListener('wa-after-show', () => { const output = $('job-output'); output.scrollTop = output.scrollHeight; });
function jobTitle(job) {
  const allocation = job.allocation || (job.selection ? [{ profile: job.selection.model || job.selection.mode }] : []);
  return allocation.length ? 'Applying ' + allocation.map((item) => labels()[item.profile] || item.profile).join(' & ') : job.restart ? 'Restarting ' + (labels()[job.restart.profile] || job.restart.profile) : job.allocation ? 'Clearing GPU Allocation' : job.action === 'benchmark' ? 'Benchmark Active Text' : job.action === 'check' ? 'Check Configuration' : 'Apply Selection';
}
// Restart keeps the live profile and GPUs; the server rejects it if either changed.
let pendingRestart;
function restartButton(item, busy) {
  const node = button('Restart', () => confirmRestart(item));
  node.size = 's'; node.dataset.runtime = item.runtime; node.disabled = busy;
  node.title = busy ? 'Wait for the current operation to finish' : 'Restart on the same GPUs';
  return node;
}
function confirmRestart(item) {
  pendingRestart = { runtime: item.runtime, profile: item.profile, devices: [...item.devices] };
  $('restart-summary').textContent = 'Restart ' + (labels()[item.profile] || item.profile) + ' on ' + item.devices.map(gpuLabel).join(' & ') + '?';
  $('restart-confirm').disabled = false; $('restart-dialog').open = true;
}
async function submitRestart() {
  if (!pendingRestart) return;
  const selected = pendingRestart; pendingRestart = null;
  $('restart-confirm').disabled = true; $('restart-dialog').open = false; showError('');
  try { const job = await api('restart', selected); jobId = job.id; renderJob(job, true); await refresh(); }
  catch (error) { showError(error.message); }
}
$('restart-confirm').addEventListener('click', submitRestart);
async function loadHistory() {
  try {
    const jobs = await api('jobs');
    const dropdown = $('recent-operations');
    const signature = JSON.stringify(jobs.map((job) => [job.id, job.state, job.started_at, jobTitle(job)]));
    // Leave an open menu alone so polling never removes the focused menu item.
    if (signature === historySignature || dropdown.open) return;
    historySignature = signature;
    dropdown.querySelectorAll('wa-dropdown-item').forEach((item) => item.remove());
    for (const job of jobs) {
      const item = element('wa-dropdown-item', jobTitle(job));
      item.dataset.jobId = job.id;
      item.append(element('span', ({ running: 'Running', queued: 'Queued', completed: 'Completed', failed: 'Failed' }[job.state] || job.state) + ' · ' + new Date(job.started_at * 1000).toLocaleTimeString(), 'history-time wa-font-size-s wa-color-text-quiet'));
      dropdown.append(item);
    }
    if (!jobs.length) { const item = element('wa-dropdown-item', 'No Recent Operations'); item.disabled = true; dropdown.append(item); }
  } catch (error) { if (error.status !== 401) showError('Could not load operation history. ' + error.message); }
}
$('recent-operations').addEventListener('wa-after-hide', () => loadHistory());
$('recent-operations').addEventListener('wa-select', async (event) => {
  const id = event.detail?.item?.dataset.jobId;
  if (!id) return;
  try { renderJob(await api('jobs/' + id), true, true); }
  catch (error) { if (error.status !== 401) showError('Could not load operation output. ' + error.message); }
});
async function refresh() {
  if (refreshing) return false;
  refreshing = true;
  try {
    let status;
    try { status = await api('status'); }
    catch (error) {
      $('updated').dataset.state = 'unavailable'; $('updated').textContent = 'Status Unavailable';
      $('apply-board').disabled = true;
      for (const button of document.querySelectorAll('#live-gpus wa-button[data-runtime]')) button.disabled = true;
      if (error.status !== 401) showConnection('Could not refresh live status. ' + error.message);
      return false;
    }
    showConnection(); renderStatus(status);
    if (jobId) {
      try {
        const job = await api('jobs/' + jobId); renderJob(job); await loadHistory();
        if (job.state !== 'running' && job.state !== 'queued') { jobId = null; await preview(); }
      } catch (error) { if (error.status !== 401) showError('Could not refresh the current operation. ' + error.message); }
    }
    return true;
  } finally { refreshing = false; }
}
async function load() {
  if (loading) return;
  loading = true; showConnection('Connecting…', true);
  try {
    await api('session'); catalogue = await api('catalogue');
    renderTray(); initialised = false; jobId = null; lastJob = null; viewedJobId = undefined; viewedJob = undefined; historySignature = undefined; plan = null;
    $('operation').open = false; $('operation-status').hidden = true;
    $('reset-board').disabled = true; $('clear-board').disabled = true; $('apply-board').disabled = true;
    showSession(true); showError('');
    if (await refresh()) await loadHistory();
  } catch (error) { if (error.status !== 401) showConnection('Could not connect to AI Control. ' + error.message); }
  finally { loading = false; }
}
$('retry-connection').addEventListener('click', async () => {
  showConnection('Reconnecting…', true);
  if (initialised && !$('dashboard').hidden) await refresh();
  else await load();
});
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if ($('login-submit').loading) return;
  $('login-submit').loading = true; showError('');
  try { await api('login', { username: $('username').value, password: $('password').value }); $('password').value = ''; await load(); }
  catch (error) { showError(error.message); }
  finally { $('login-submit').loading = false; }
});
async function signOut() { try { await api('logout', {}); showSession(false); } catch (error) { showError(error.message); } }
$('logout').addEventListener('click', signOut);
$('header-menu').addEventListener('wa-select', (event) => { if (event.detail.item.id === 'logout-menu') signOut(); });
$('reset-board').addEventListener('click', () => { draft = liveAllocation(); renderBoard(); preview(); });
$('clear-board').addEventListener('click', () => { draft = []; renderBoard(); preview(); });
$('apply-board').addEventListener('click', async () => {
  if (!plan || $('apply-board').loading) return; $('apply-board').disabled = true; $('apply-board').loading = true; $('allocation-message').textContent = ''; showError('');
  try { const job = await api('board', { allocation: draft, signature: plan.signature, replace: true }); jobId = job.id; renderJob(job, true); await refresh(); }
  catch (error) { $('allocation-message').textContent = 'Failed · ' + error.message; await preview(); }
  finally { $('apply-board').loading = false; }
});
if (touch()) $('workloads-hint').textContent = 'Tap a GPU button to place a workload in the preview. Choose Empty in a GPU dropdown to remove one.';
setInterval(() => { if (!$('dashboard').hidden) refresh(); }, 2000);
load();
