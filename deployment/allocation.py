"""GPU allocation validation for the host tools.

The deployment package runs on the host without the AI Control app, so it
carries its own copy of the app's rule; `tests/test_deployment_allocation.py`
keeps the two in step.
"""


def normalise_allocation(items, rules):
    if not isinstance(items, list) or len(items) > 2:
        raise ValueError('Supply up to two workload cards.')
    result, devices_used, runtimes_used = [], set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Supply a workload card object.')
        profile, devices = item.get('profile'), item.get('devices')
        if not isinstance(profile, str) or profile == 'off' or profile not in rules['profiles']:
            raise ValueError('Choose a known workload.')
        if not isinstance(devices, list) or not devices or any(not isinstance(d, str) or d not in {'0', '1'} for d in devices):
            raise ValueError('Choose GPU 0, GPU 1 or both GPUs.')
        placement = ','.join(sorted(devices))
        if placement not in rules['profiles'][profile]:
            raise ValueError('Choose a supported GPU placement for this workload.')
        name = rules['workloads'][profile]['runtime']
        if name in runtimes_used or devices_used.intersection(devices):
            raise ValueError('Workloads cannot share a GPU or repeat the same runtime.')
        result.append({'profile': profile, 'devices': sorted(devices)})
        devices_used.update(devices)
        runtimes_used.add(name)
    return sorted(result, key=lambda item: item['devices'])

