"""Validate and plan manual GPU allocations using supplied placement rules."""

import hashlib
import json


def normalise_allocation(items, rules):
    if not isinstance(items, list) or len(items) > 2:
        raise ValueError("Supply up to two workload cards.")
    result, devices_used, runtimes_used = [], set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Supply a workload card object.")  # noqa: TRY004 - callers map ValueError to HTTP 400 and CLI exit 64
        profile, devices = item.get("profile"), item.get("devices")
        if (
            not isinstance(profile, str)
            or profile == "off"
            or profile not in rules["profiles"]
        ):
            raise ValueError("Choose a known workload.")
        if (
            not isinstance(devices, list)
            or not devices
            or any(not isinstance(d, str) or d not in {"0", "1"} for d in devices)
        ):
            raise ValueError("Choose GPU 0, GPU 1 or both GPUs.")
        placement = ",".join(sorted(devices))
        if placement not in rules["profiles"][profile]:
            raise ValueError("Choose a supported GPU placement for this workload.")
        name = rules["workloads"][profile]["runtime"]
        if name in runtimes_used or devices_used.intersection(devices):
            raise ValueError("Workloads cannot share a GPU or repeat the same runtime.")
        result.append({"profile": profile, "devices": sorted(devices)})
        devices_used.update(devices)
        runtimes_used.add(name)
    return sorted(result, key=lambda item: item["devices"])


def allocation_plan(allocation, status, *, rules, stopping=(), complete=True):
    """Plan a complete board or a partial selection using supplied placement rules."""
    current = status.get("runtimes", {})
    targets = [
        dict(item, runtime=rules["workloads"][item["profile"]]["runtime"])
        for item in allocation
    ]
    keep = [
        item["runtime"]
        for item in targets
        if (active := current.get(item["runtime"], {})).get("running")
        and active.get("profile") == item["profile"]
        and active.get("devices") == item["devices"]
        and active.get("health") == "healthy"
        and active.get("config_current") is True
    ]
    names = {item["runtime"] for item in targets}
    devices = {device for item in targets for device in item["devices"]}
    stop = []
    for name in rules["runtime_names"]:
        active = current.get(name, {})
        if not active.get("running") or name in keep:
            continue
        if (
            complete
            or name in stopping
            or name in names
            or devices.intersection(active.get("devices", []))
        ):
            stop.append(name)
    signature = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
    return {
        "allocation": allocation,
        "keep": keep,
        "start": [item for item in targets if item["runtime"] not in keep],
        "stop": stop,
        "signature": signature,
    }
