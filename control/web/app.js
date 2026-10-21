let draggedBoardProfile;
let catalogue, live, draft = [], initialised = false, jobId, lastJob, plan, planVersion = 0, refreshing = false;
const $ = (id) => document.getElementById(id);
const gpuLabel = (device) => 'GPU ' + (Number(device) + 1);
const runtime = (profile) => catalogue.rules.workloads[profile].runtime;
const runtimeName = (name) => catalogue.rules.runtime_names[name];
const showError = (message) => { $('error').textContent = message; $('error').hidden = !message; };
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
  return Object.values(live.runtimes).filter((item) => item.running).map((item) => ({ profile: item.profile, devices: [...item.devices] }));
}
function canonical(items) { return JSON.stringify([...items].sort((a, b) => a.devices.join().localeCompare(b.devices.join()))); }
function showSession(signedIn) {
  if (!signedIn) { $('operation').open = false; $('recent-operations').open = false; $('header-menu').open = false; }
  $('dashboard').hidden = !signedIn; $('login').hidden = signedIn;
  for (const id of ['logout', 'logout-menu', 'recent-operations']) $(id).hidden = !signedIn;
}
async function api(path, payload) {
  const response = await fetch('/api/' + path, { method: payload === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-AI-Control': '1' }, body: payload === undefined ? undefined : JSON.stringify(payload) });
  if (response.status === 401) showSession(false);
  const text = await response.text();
  let data; try { data = JSON.parse(text); } catch { data = { error: text }; }
  if (!response.ok) throw new Error(data.error || 'The control request failed.');
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
  const board = $('gpu-board'); board.replaceChildren();
  for (const device of ['0', '1']) {
    const item = draft.find((value) => value.devices.includes(device));
    const shared = item?.devices.length === 2;
    if (shared && device !== item.devices[0]) continue;
    const card = element('wa-card', undefined, 'workload-card gpu-board-card' + (item ? ' allocated' : '') + (shared ? ' dual' : ''));
    const content = cardContent(card);
    card.dataset.device = device; card.setAttribute('aria-label', (shared ? 'Both GPUs' : gpuLabel(device)) + ' Preview & Drop Zone');
    const heading = element('div', undefined, 'gpu-status-heading wa-cluster wa-gap-s'); const title = element('h3', shared ? 'GPU 1 & GPU 2' : gpuLabel(device)); heading.append(title);
    const descriptions = [...new Set((shared ? item.devices : [device]).map(hardware).filter(Boolean))];
    if (descriptions.length) heading.append(element('span', descriptions.join(' / '), 'hint hardware-name board-hardware'));
    const select = element('wa-select'); select.id = 'gpu-select-' + device; select.setAttribute('aria-label', (shared ? 'Both GPUs' : gpuLabel(device)) + ' Workload');
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
    content.append(heading, select, element('span', item ? item.devices.length === 2 ? 'Shared Across Both GPUs' : 'One GPU' : touch() ? 'Choose a workload above' : 'Drop a workload here', 'hint'));
    const devices = shared ? item.devices : [device];
    for (const number of devices) { const thermal = element('span', undefined, 'hint gpu-hardware'); thermal.dataset.device = number; content.append(thermal); }
    dropzone(card, device); if (item) draggable(card, item.profile); board.append(card);
  }
  renderBoardTemperatures(); updatePlacementButtons();
  $('board-state').textContent = live && canonical(draft) === canonical(liveAllocation()) ? 'Matches Live Allocation' : 'Unapplied Preview';
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
    const container = element('div', undefined, 'workload-group wa-stack wa-gap-s'); container.append(element('h3', group));
    for (const item of workloads().filter((value) => value.group === group)) {
      const card = element('wa-card', undefined, 'workload-card');
      const content = cardContent(card);
      const allowed = catalogue.rules.profiles[item.profile];
      const title = element('h4', item.label);
      content.append(title, element('span', allowed.length === 1 ? (allowed[0] === '0,1' ? 'Requires Both GPUs' : gpuLabel(allowed[0]) + ' Only') : (allowed.includes('0,1') ? 'One Or Both GPUs' : 'One GPU'), 'hint'));
      const controls = element('div', undefined, 'card-actions wa-cluster wa-gap-xs');
      for (const placement of allowed) {
        const choice = button(placement === '0,1' ? 'Both GPUs' : gpuLabel(placement), () => place(item.profile, placement));
        choice.size = 'm';
        if (touch()) { choice.variant = 'brand'; choice.appearance = 'filled-outlined'; }
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
    choice.disabled = choice.dataset.devices !== '0,1' && used.has(choice.dataset.devices);
    choice.title = choice.disabled ? 'Occupied in the preview. Use a GPU dropdown or drag to replace it.' : 'Place in the preview';
  }
}
async function preview() {
  const version = ++planVersion; plan = null; $('apply-board').disabled = true; $('change-preview').textContent = 'Checking the allocation…';
  try {
    const result = await api('board/plan', { allocation: draft });
    if (version !== planVersion) return;
    plan = result; const container = $('change-preview'); container.replaceChildren();
    if (!result.stop.length && !result.start.length) container.append(element('p', 'No changes needed.'));
    else {
      if (result.stop.length) container.append(element('p', 'Will stop: ' + result.stop.map((name) => name === 'text' ? live.text_model : runtimeName(name)).join('; ') + '.'));
      if (result.start.length) container.append(element('p', 'Will start: ' + result.start.map((item) => (labels()[item.profile] || item.profile) + ' on ' + item.devices.map(gpuLabel).join(', ')).join('; ') + '.'));
      if (result.keep.length) container.append(element('p', 'Keeps running: ' + result.keep.map((name) => runtimeName(name)).join(', ') + '.'));
    }
    $('apply-board').disabled = Boolean(live?.job) || (!result.stop.length && !result.start.length);
  } catch (error) { if (version === planVersion) $('change-preview').textContent = 'Failed · ' + error.message; }
}
function metricBar(label, value, maximum, text) {
  const metric = element('div', undefined, 'gpu-metric wa-stack wa-gap-xs');
  const heading = element('div', undefined, 'metric-heading wa-split wa-flex-wrap wa-gap-xs');
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
  const container = $('host-status'); container.replaceChildren(); $('disk-status').replaceChildren();
  const elapsed = current?.sampled_at_seconds - previousHost?.sampled_at_seconds;
  function rate(group, field) {
    const next = current?.[group], before = previousHost?.[group];
    const unchanged = group === 'disk' ? next?.device && next.device === before?.device : next?.interface && next.interface === before?.interface;
    const delta = next?.[field] - before?.[field];
    return unchanged && elapsed > 0 && Number.isFinite(delta) && delta >= 0 ? byteSize(delta / elapsed) + '/s' : Number.isFinite(next?.[field]) ? 'Sampling…' : 'Unavailable';
  }
  function card(title, hint, target = container) {
    const node = element('wa-card', undefined, 'host-card'); node.dataset.kind = title.toLowerCase();
    const content = cardContent(node);
    const heading = element('div', undefined, 'metric-heading wa-split wa-flex-wrap wa-gap-xs'); const titleNode = element('h3', title); heading.append(titleNode, element('span', hint, 'hardware-name')); content.append(heading); target.append(node); return content;
  }
  function transfers(node, first, second) {
    const row = element('div', undefined, 'transfer-rates wa-grid wa-gap-s');
    for (const [label, value] of [first, second]) { const item = element('div', undefined, 'wa-stack wa-gap-xs'); item.append(element('span', label, 'hint'), element('strong', value)); row.append(item); }
    node.append(row);
  }
  const total = current?.cpu?.total_ticks - previousHost?.cpu?.total_ticks;
  const idle = current?.cpu?.idle_ticks - previousHost?.cpu?.idle_ticks;
  const busy = elapsed > 0 && total > 0 && idle >= 0 ? Math.min(100, Math.max(0, (total - idle) / total * 100)) : undefined;
  const cpu = card('CPU', current?.cpu?.name || '');
  cpu.append(metricBar('Activity', busy, 100, Number.isFinite(busy) ? busy.toFixed(1) + '%' : Number.isFinite(current?.cpu?.total_ticks) ? 'Sampling…' : 'Unavailable'));
  const cpuDetail = [current?.cpu?.cores ? current.cpu.cores + ' Threads' : '', temperature(current?.cpu?.temperature_celsius)].filter(Boolean).join(' · ');
  if (cpuDetail) cpu.append(element('span', cpuDetail, 'hint'));
  const memory = current?.memory;
  const ram = card('Memory', 'RAM');
  ram.append(metricBar('Used', memory?.used_bytes, memory?.total_bytes, memory?.total_bytes > 0 ? byteSize(memory.used_bytes) + ' / ' + byteSize(memory.total_bytes) : 'Unavailable'));
  if (Number.isFinite(memory?.available_bytes)) ram.append(element('span', byteSize(memory.available_bytes) + ' Available', 'hint disk-free'));
  const disk = current?.disk;
  const storage = card('Disk', disk?.path || 'AI Data', $('disk-status'));
  storage.append(metricBar('Used', disk?.used_bytes, disk?.total_bytes, disk?.total_bytes > 0 ? byteSize(disk.used_bytes) + ' / ' + byteSize(disk.total_bytes) : 'Unavailable'));
  if (Number.isFinite(disk?.free_bytes)) storage.append(element('span', byteSize(disk.free_bytes) + ' Free', 'hint disk-free'));
  transfers(storage, ['Read', rate('disk', 'read_bytes')], ['Write', rate('disk', 'write_bytes')]);
  transfers(card('Network', current?.network?.interface || ''), ['Receive', rate('network', 'received_bytes')], ['Send', rate('network', 'sent_bytes')]);
  previousHost = current;
}
function renderStatus(data) {
  const followsLive = !live || canonical(draft) === canonical(liveAllocation());
  const changed = live && canonical(Object.values(live.runtimes).filter((item) => item.running).map((item) => ({ profile: item.profile, devices: item.devices }))) !== canonical(Object.values(data.runtimes).filter((item) => item.running).map((item) => ({ profile: item.profile, devices: item.devices })));
  live = data; renderHost(data.host_metrics); $('live-gpus').replaceChildren();
  for (const device of ['0', '1']) {
    const metrics = data.gpu_metrics?.[device];
    const column = element('wa-card', undefined, 'gpu-status'); column.dataset.device = device;
    const content = cardContent(column);
    const heading = element('div', undefined, 'gpu-status-heading wa-cluster wa-gap-s'); const number = element('h3', gpuLabel(device)); heading.append(number); if (metrics?.name) heading.append(element('span', metrics.name, 'hint hardware-name')); content.append(heading);
    const owners = Object.entries(data.runtimes).filter(([, item]) => item.running && item.devices.includes(device)).map(([name, item]) => ({ ...item, runtime: name }));
    const health = owners.length ? (owners.every((item) => item.health === 'healthy') ? 'Ready' : owners.find((item) => item.health !== 'healthy').health) : 'Idle';
    const badge = element('wa-badge', health === 'starting' ? 'Starting' : health, 'health-badge'); badge.variant = health === 'Ready' ? 'success' : health === 'starting' ? 'warning' : 'neutral'; badge.appearance = 'outlined'; heading.append(badge);
    if (!owners.length) content.append(element('strong', data['gpu' + device], 'gpu-workload'));
    for (const item of owners) {
      const row = element('div', undefined, 'gpu-owner wa-split wa-flex-wrap wa-gap-s');
      row.append(element('strong', labels()[item.profile] || item.profile, 'gpu-workload'), restartButton(item, Boolean(data.job)));
      content.append(row);
    }
    const memory = metrics?.vram_total_bytes > 0 && Number.isFinite(metrics?.vram_used_bytes);
    content.append(metricBar('Activity', metrics?.busy_percent, 100, Number.isFinite(metrics?.busy_percent) ? metrics.busy_percent + '%' : 'Unavailable'));
    content.append(metricBar('VRAM', memory ? metrics.vram_used_bytes : undefined, metrics?.vram_total_bytes, memory ? (metrics.vram_used_bytes / 2 ** 30).toFixed(1) + ' / ' + (metrics.vram_total_bytes / 2 ** 30).toFixed(1) + ' GiB' : 'Unavailable'));
    const thermal = [temperature(metrics?.temperature_celsius), Number.isFinite(metrics?.hotspot_celsius) ? 'Hotspot ' + temperature(metrics.hotspot_celsius) : ''].filter(Boolean).join(' · ');
    if (thermal) content.append(element('span', thermal, 'hint gpu-temperature'));
    $('live-gpus').append(column);
  }
  for (const link of document.querySelectorAll('#services wa-button')) {
    const service = catalogue.services.find((item) => item.title === link.dataset.service);
    link.disabled = Boolean(service.container && (!data.services?.[service.container]?.running || service.profiles && !service.profiles.includes(data.text_profile) || service.backends && !service.backends.some((name) => data.services?.[name]?.running)));
    link.title = link.disabled ? 'Service Unavailable' : service.title;
  }
  renderBoardTemperatures();
  $('updated').dataset.state = 'live';
  $('updated').textContent = 'Live · ' + new Date(data.updated).toLocaleTimeString();
  if (data.job) { jobId = data.job.id; $('apply-board').disabled = true; }
  if (!initialised) { initialised = true; draft = liveAllocation(); renderBoard(); preview(); }
  else if (changed) { if (followsLive) draft = liveAllocation(); renderBoard(); preview(); }
  else if (!data.job && plan) $('apply-board').disabled = !plan.stop.length && !plan.start.length;
}
function renderJob(job, open = false) {
  const changed = lastJob?.id !== job.id;
  const newlyRunning = job.state === 'running' && lastJob?.id !== job.id;
  lastJob = job;
  if ((open || newlyRunning) && !$('operation').open) $('operation').open = true;
  $('operation').label = jobTitle(job);
  const message = ({ queued: 'Queued', running: 'Running', completed: 'Completed', failed: 'Failed' }[job.state] || 'Needs Attention') + (job.stage ? ' · ' + job.stage : '');
  const feedback = job.allocation ? $('allocation-message') : null;
  if (feedback) feedback.textContent = message;
  const end = job.finished_at || Date.now() / 1000;
  const seconds = Math.max(0, Math.floor(end - job.started_at));
  const age = Math.max(0, Math.floor(Date.now() / 1000 - (job.updated_at || job.started_at)));
  $('job-time').textContent = Math.floor(seconds / 60) + 'm ' + seconds % 60 + 's' + (job.state === 'running' ? ' · Last Output ' + age + 's Ago' : ' · ' + (job.state === 'completed' ? 'Completed' : 'Failed'));
  const output = $('job-output'); const follow = changed || open || output.scrollHeight - output.scrollTop - output.clientHeight < 40;
  const text = job.output || 'Waiting for output…';
  if (output.textContent !== text) output.textContent = text;
  if (follow) requestAnimationFrame(() => { if (lastJob?.id === job.id) output.scrollTop = output.scrollHeight; });
}
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
    dropdown.querySelectorAll('wa-dropdown-item').forEach((item) => item.remove());
    for (const job of jobs) {
      const item = element('wa-dropdown-item', jobTitle(job));
      item.append(element('span', ({ running: 'Running', completed: 'Completed', failed: 'Failed' }[job.state] || job.state) + ' · ' + new Date(job.started_at * 1000).toLocaleTimeString(), 'history-time'));
      item.addEventListener('click', async () => {
        dropdown.open = false;
        try { renderJob(await api('jobs/' + job.id), true); } catch (error) { showError(error.message); }
      });
      dropdown.append(item);
    }
    if (!jobs.length) { const item = element('wa-dropdown-item', 'No Recent Operations'); item.disabled = true; dropdown.append(item); }
  } catch (error) { showError(error.message); }
}
async function refresh() {
  if (refreshing) return; refreshing = true;
  try {
    const status = await api('status'); renderStatus(status);
    if (jobId) {
      const job = await api('jobs/' + jobId); renderJob(job); await loadHistory();
      if (job.state !== 'running') { jobId = null; renderStatus(await api('status')); await preview(); }
    }
  } catch (error) { $('updated').dataset.state = 'unavailable'; $('updated').textContent = 'Status Unavailable'; $('apply-board').disabled = true; showError(error.message); }
  finally { refreshing = false; }
}
async function load() {
  try {
    await api('session'); catalogue = await api('catalogue');
    $('services').replaceChildren();
    for (const title of [...new Set(catalogue.services.map((item) => item.group))].sort()) {
      const group = element('wa-card', undefined, 'service-group');
      const content = cardContent(group);
      content.append(element('h2', title));
      const links = element('div', undefined, 'service-links wa-cluster wa-gap-s');
      for (const item of catalogue.services.filter((item) => item.group === title).sort((a, b) => a.title.localeCompare(b.title))) {
        const link = element('wa-button', item.display_label);
        link.title = item.title; link.setAttribute('aria-label', item.title);
        link.dataset.service = item.title; link.disabled = Boolean(item.container); link.appearance = 'outlined';
        link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link);
      }
      content.append(links); $('services').append(group);
    }
    renderTray(); initialised = false; jobId = null; lastJob = null; plan = null; $('operation').open = false;
    showSession(true); showError(''); await refresh(); await loadHistory();
  } catch (error) { if ($('login').hidden) showError(error.message); }
}
$('login-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await api('login', { username: $('username').value, password: $('password').value }); $('password').value = ''; await load(); } catch (error) { showError(error.message); } });
async function signOut() { await api('logout', {}); showSession(false); }
$('logout').addEventListener('click', signOut);
$('header-menu').addEventListener('wa-select', (event) => { if (event.detail.item.id === 'logout-menu') signOut(); });
$('reset-board').addEventListener('click', () => { draft = liveAllocation(); renderBoard(); preview(); });
$('clear-board').addEventListener('click', () => { draft = []; renderBoard(); preview(); });
$('apply-board').addEventListener('click', async () => {
  if (!plan) return; $('apply-board').disabled = true; showError('');
  try { const job = await api('board', { allocation: draft, signature: plan.signature, replace: true }); jobId = job.id; renderJob(job, true); await refresh(); }
  catch (error) { $('allocation-message').textContent = 'Failed · ' + error.message; await preview(); }
});
// Keep GPU allocation within reach on phones; metrics remain one tap away.
$('status-panel').open = !window.matchMedia('(max-width: 600px)').matches;
if (touch()) $('workloads-hint').textContent = 'Tap a GPU button to place a workload in the preview. Choose Empty in a GPU dropdown to remove one.';
setInterval(() => { if (!$('dashboard').hidden) refresh(); }, 2000);
load();
