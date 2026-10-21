"""Resolve canonical Compose workloads into externally supplied engine API specs."""
from decimal import Decimal
from pathlib import Path
import os
import re
import shlex
import subprocess

import yaml

from deployment import runtime
from deployment.address import http_url


def duration(value):
    """Convert a Compose duration to the engine API's integer nanoseconds."""
    if isinstance(value, (int, float)):
        return int(Decimal(str(value)) * 1_000_000_000)
    units = {'h': 3_600_000_000_000, 'm': 60_000_000_000, 's': 1_000_000_000,
             'ms': 1_000_000, 'us': 1_000, 'µs': 1_000, 'ns': 1}
    matches = list(re.finditer(r'(\d+(?:\.\d+)?)(ns|us|µs|ms|h|m|s)', str(value)))
    if ''.join(match.group() for match in matches) != str(value):
        raise ValueError('Unsupported Compose duration.')
    return int(sum(Decimal(match[1]) * units[match[2]] for match in matches))


def memory(value):
    if isinstance(value, int):
        return value
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([kmgt]?)b?', str(value).lower())
    if not match:
        raise ValueError('Unsupported Compose memory limit.')
    return int(Decimal(match[1]) * 1024 ** ('kmgt'.index(match[2]) + 1 if match[2] else 0))


def arguments(value):
    return shlex.split(value) if isinstance(value, str) else value


def container_spec(service, profile):
    """Translate a resolved service, rejecting unresolved host bind paths."""
    supported = {'cap_add', 'command', 'container_name', 'devices', 'entrypoint', 'environment',
                 'group_add', 'healthcheck', 'hostname', 'image', 'init', 'ipc', 'labels',
                 'mem_limit', 'network_mode', 'networks', 'ports', 'profiles', 'restart',
                 'security_opt', 'shm_size', 'stop_grace_period', 'user', 'userns_mode',
                 'volumes', 'working_dir'}
    if unsupported := service.keys() - supported:
        raise ValueError('Unsupported Compose service fields: ' + ', '.join(sorted(unsupported)))
    # Compose inserts this implicit network; custom network attachment needs explicit support.
    if service.get('networks') not in (None, {'default': None}, {'default': {}}):
        raise ValueError('Custom Compose networks are unsupported; use network_mode.')
    config = {'Image': service['image'], 'Labels': {'com.docker.compose.service': profile}}
    host = config['HostConfig'] = {}
    for source, target in [('command', 'Cmd'), ('entrypoint', 'Entrypoint')]:
        if source in service and service[source] is not None:
            config[target] = arguments(service[source])
    environment = service.get('environment', {})
    if isinstance(environment, dict):
        if any(value is None for value in environment.values()):
            raise ValueError('Compose environment contains an unresolved value.')
        config['Env'] = [f'{key}={value}' for key, value in environment.items()]
    else:
        config['Env'] = list(environment)
    labels = service.get('labels', {})
    config['Labels'].update(labels if isinstance(labels, dict) else dict(item.split('=', 1) for item in labels))
    for source, target in [('user', 'User'), ('working_dir', 'WorkingDir'), ('hostname', 'Hostname')]:
        if source in service:
            config[target] = str(service[source])
    for source, target in [('network_mode', 'NetworkMode'), ('ipc', 'IpcMode'), ('cap_add', 'CapAdd'),
                           ('security_opt', 'SecurityOpt'), ('init', 'Init'), ('group_add', 'GroupAdd'),
                           ('userns_mode', 'UsernsMode'), ('shm_size', 'ShmSize')]:
        if source in service:
            host[target] = memory(service[source]) if source == 'shm_size' else service[source]
    if 'mem_limit' in service:
        host['Memory'] = memory(service['mem_limit'])
    if 'restart' in service:
        name, _, count = service['restart'].partition(':')
        host['RestartPolicy'] = {'Name': name, 'MaximumRetryCount': int(count) if count else 0}
    binds = []
    for volume in service.get('volumes', []):
        if isinstance(volume, str):
            source, target, *mode = volume.split(':')
            options = mode[0] if mode else 'rw'
        else:
            if volume['type'] != 'bind':
                raise ValueError('Only explicit host bind mounts are supported.')
            source, target = volume['source'], volume['target']
            options = 'ro' if volume.get('read_only') else 'rw'
            if propagation := volume.get('bind', {}).get('propagation'):
                options += ',' + propagation
        if not Path(source).is_absolute():
            raise ValueError('Compose host bind paths must be absolute.')
        binds.append(f'{source}:{target}:{options}')
    if binds:
        host['Binds'] = binds
    devices = []
    for device in service.get('devices', []):
        if isinstance(device, str):
            source, *rest = device.split(':')
            target = rest[0] if rest else source
            permissions = rest[1] if len(rest) > 1 else 'rwm'
        else:
            source, target = device['source'], device['target']
            permissions = device.get('permissions', 'rwm')
        devices.append({'PathOnHost': source, 'PathInContainer': target, 'CgroupPermissions': permissions})
    if devices:
        host['Devices'] = devices
    exposed, bindings = {}, {}
    for port in service.get('ports', []):
        if isinstance(port, (str, int)):
            address, *protocol = str(port).split('/')
            parts = address.rsplit(':', 2)
            target = parts[-1]
            published = parts[-2] if len(parts) > 1 else ''
            address = parts[0].strip('[]') if len(parts) == 3 else ''
            protocol = protocol[0] if protocol else 'tcp'
        else:
            target, published = port['target'], str(port.get('published', ''))
            address, protocol = port.get('host_ip', ''), port.get('protocol', 'tcp')
        key = f'{target}/{protocol}'
        exposed[key] = {}
        bindings.setdefault(key, []).append({'HostIp': address, 'HostPort': published})
    if exposed:
        config['ExposedPorts'], host['PortBindings'] = exposed, bindings
    if 'healthcheck' in service:
        health = service['healthcheck']
        translated = {'Test': ['NONE']} if health.get('disable') else {}
        if 'test' in health and not health.get('disable'):
            translated['Test'] = arguments(health['test'])
        for source, target in [('interval', 'Interval'), ('timeout', 'Timeout'), ('start_period', 'StartPeriod'), ('start_interval', 'StartInterval')]:
            if source in health:
                translated[target] = duration(health[source])
        if 'retries' in health:
            translated['Retries'] = int(health['retries'])
        config['Healthcheck'] = translated
    if 'stop_grace_period' in service:
        config['StopTimeout'] = duration(service['stop_grace_period']) // 1_000_000_000
    return {'name': service['container_name'], 'config': config}


def placement_environment(profile, devices):
    from deployment.workloads import CONTAINERS, PROFILES, WORKLOADS

    if devices not in WORKLOADS[profile]['placements']:
        raise ValueError(f'{profile} requires a supported placement.')
    workload_runtime, variable = PROFILES[profile]
    environment = dict(os.environ, GPU_CONTAINER=CONTAINERS[workload_runtime])
    if variable:
        environment[variable] = devices
    environment.update(WORKLOADS[profile].get('environment', {}).get(devices, {}))
    environment.update(AI_GPU_DEVICES=devices, HIP_VISIBLE_DEVICES=devices,
                       AI_GPU_LEASE_DIR=str(Path(os.environ['AI_DATA_DIR']) / 'control/leases'))
    return environment


def resolve(root):
    from deployment.workloads import WORKLOADS

    selected = runtime.engine()

    def render(profile, environment):
        arguments = ['--project-directory', str(root)] if selected == 'docker' else []
        rendered = subprocess.run(runtime.compose(*arguments, '--profile', profile, 'config'),
                                  cwd=root, env=environment, check=True,
                                  capture_output=True, text=True).stdout
        document = yaml.safe_load(rendered)
        return document

    def normalise(service):
        if selected == 'docker':
            # Docker serialises literal dollars escaped for a second Compose parse.
            def unescape(value):
                if isinstance(value, str):
                    return value.replace('$$', '$')
                if isinstance(value, list):
                    return [unescape(item) for item in value]
                if isinstance(value, dict):
                    return {key: unescape(item) for key, item in value.items()}
                return value
            service = unescape(service)
        for volume in service.get('volumes', []):
            if isinstance(volume, dict) and volume.get('type') == 'bind':
                volume['source'] = str((root / volume['source']).resolve())
        # Podman Compose retains short-form relative mounts in its rendered model.
        service['volumes'] = [str((root / item.split(':', 1)[0]).resolve()) + ':' + item.split(':', 1)[1]
                              if isinstance(item, str) else item for item in service.get('volumes', [])]
        return service

    workloads = {}
    for profile, workload in WORKLOADS.items():
        placements = {}
        for devices in workload['placements']:
            service = render(profile, placement_environment(profile, devices))['services'][profile]
            placements[devices] = container_spec(normalise(service), profile)
        workloads[profile] = {'placements': placements}
    key = os.environ['RADIANCE_API_KEY']
    benchmark_environment = dict(os.environ, BETTERBENCH_API_KEY=key)
    service = normalise(render('benchmark', benchmark_environment)['services']['betterbench'])
    service['container_name'] = 'ai-benchmark'
    return {'workloads': workloads, 'benchmark': {
        'spec': container_spec(service, 'betterbench'),
        'endpoint': http_url(os.environ['AI_HOST'], 8000) + '/v1',
        'api_key': key,
    }}
