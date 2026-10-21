"""Read optional GPU and host telemetry from explicitly supplied host mounts."""
from pathlib import Path
import shutil


def temperature_celsius(hwmons, labels):
    readings = {}
    for directory in hwmons:
        for sensor in sorted(directory.glob('temp*_input')):
            try:
                label = sensor.with_name(sensor.name.replace('_input', '_label'))
                name = label.read_text().strip() if label.exists() else sensor.name.removesuffix('_input')
                readings.setdefault(name, []).append(int(sensor.read_text()) / 1000)
            except (OSError, ValueError):
                continue
    return next((max(readings[label]) for label in labels if readings.get(label)), None)


def gpu_metrics(sys_root, pci_ids=Path('/usr/share/misc/pci.ids')):
    sys_root, pci_ids = Path(sys_root), Path(pci_ids)
    result = {}
    names = {}
    try:
        vendor = ''
        for line in pci_ids.read_text().splitlines():
            if line and not line.startswith(('#', '\t')) and len(line.split(None, 1)) == 2:
                vendor = line.split(None, 1)[0]
            elif line.startswith('\t') and not line.startswith('\t\t') and len(line.split(None, 1)) == 2:
                identifier, name = line.split(None, 1)
                names[(vendor, identifier)] = name.rsplit('[', 1)[-1].rstrip(']') if '[' in name else name
    except OSError:
        pass
    try:
        nodes = sorted((sys_root / 'class/kfd/kfd/topology/nodes').iterdir(), key=lambda path: int(path.name))
        devices = []
        for node in nodes:
            values = dict(line.split(' ', 1) for line in (node / 'properties').read_text().splitlines() if ' ' in line)
            if int(values.get('simd_count', 0)) > 0:
                devices.append(values['drm_render_minor'])
        # ROCm's GPU ordinals follow KFD GPU nodes; DRM card numbers can change at boot.
        for ordinal, minor in enumerate(devices[:2]):
            device = sys_root / f'class/drm/renderD{minor}/device'
            metrics = {}
            for field, filename in [('busy_percent', 'gpu_busy_percent'), ('vram_total_bytes', 'mem_info_vram_total'), ('vram_used_bytes', 'mem_info_vram_used')]:
                try:
                    metrics[field] = int((device / filename).read_text())
                except (OSError, ValueError):
                    metrics[field] = None
            try:
                identifier = tuple((device / field).read_text().strip().removeprefix('0x') for field in ['vendor', 'device'])
                metrics['name'] = names.get(identifier)
            except OSError:
                metrics['name'] = None
            hwmons = list((device / 'hwmon').glob('hwmon*'))
            metrics['temperature_celsius'] = temperature_celsius(hwmons, ['edge', 'temp1'])
            metrics['hotspot_celsius'] = temperature_celsius(hwmons, ['junction'])
            result[str(ordinal)] = metrics
    except (OSError, ValueError, KeyError):
        pass
    return result


def host_metrics(root, proc_root, sys_root):
    root, proc_root, sys_root = Path(root), Path(proc_root), Path(sys_root)
    result = {'cpu': {}, 'disk': {}, 'memory': {}, 'network': {}}
    try:
        result['sampled_at_seconds'] = float((proc_root / 'uptime').read_text().split()[0])
        lines = (proc_root / 'stat').read_text().splitlines()
        ticks = list(map(int, lines[0].split()[1:9]))
        result['cpu'] = {'cores': sum(line.startswith('cpu') and line[3:4].isdigit() for line in lines), 'idle_ticks': ticks[3] + ticks[4], 'total_ticks': sum(ticks)}
    except (OSError, ValueError, IndexError):
        pass
    try:
        result['cpu']['name'] = next(line.split(':', 1)[1].strip() for line in (proc_root / 'cpuinfo').read_text().splitlines() if line.startswith('model name'))
    except (OSError, StopIteration):
        pass
    hwmons = []
    for directory in (sys_root / 'class/hwmon').glob('hwmon*'):
        try:
            if (directory / 'name').read_text().strip() in {'coretemp', 'k10temp', 'zenpower'}:
                hwmons.append(directory)
        except OSError:
            continue
    temperature = temperature_celsius(hwmons, ['Tdie', 'Tctl', 'Package id 0', 'temp1'])
    if temperature is not None:
        result['cpu']['temperature_celsius'] = temperature
    try:
        values = {line.split(':', 1)[0]: int(line.split()[1]) * 1024 for line in (proc_root / 'meminfo').read_text().splitlines() if ':' in line}
        total, available = values['MemTotal'], values['MemAvailable']
        result['memory'] = {'available_bytes': available, 'total_bytes': total, 'used_bytes': max(0, total - available)}
    except (OSError, ValueError, IndexError, KeyError):
        pass
    try:
        usage = shutil.disk_usage(root)
        result['disk'] = {'path': str(root), 'total_bytes': usage.total, 'used_bytes': usage.used, 'free_bytes': usage.free}
        # Use only the filesystem backing AI data; summing devices double-counts partitions.
        mounts = []
        for line in (proc_root / 'self/mountinfo').read_text().splitlines():
            fields, filesystem = line.split(' - ', 1)
            mount = fields.split()[4].replace('\\040', ' ').replace('\\134', '\\')
            if root.resolve().is_relative_to(Path(mount)):
                mounts.append((len(mount), filesystem.split()[1]))
        source = max(mounts)[1]
        device = Path(source).resolve().name
        values = list(map(int, (sys_root / 'class/block' / device / 'stat').read_text().split()))
        result['disk'].update(device=device, read_bytes=values[2] * 512, write_bytes=values[6] * 512)
    except (OSError, ValueError, IndexError):
        pass
    try:
        # The default-route interface excludes loopback, containers and duplicate tunnel traffic.
        routes = [line.split() for line in (proc_root / '1/net/route').read_text().splitlines()[1:]]
        interface = min((row for row in routes if row[1] == '00000000' and row[7] == '00000000' and int(row[3], 16) & 1), key=lambda row: int(row[6]))[0]
        statistics = sys_root / 'class/net' / interface / 'statistics'
        result['network'] = {'interface': interface, 'received_bytes': int((statistics / 'rx_bytes').read_text()), 'sent_bytes': int((statistics / 'tx_bytes').read_text())}
    except (OSError, ValueError, IndexError):
        pass
    return result
