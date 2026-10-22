"""Synthetic deployment data: no host inventory or model catalogue is imported."""


def synthetic_catalogue():
    workloads = {
        "dual-text": {"kind": "text", "label": "Dual Text", "runtime": "text"},
        "single-text": {"kind": "text", "label": "Single Text", "runtime": "text"},
        "render-model": {
            "kind": "image",
            "label": "Render Model",
            "runtime": "render-pool",
        },
        "motion-model": {
            "kind": "video",
            "label": "Motion Model",
            "runtime": "motion-pool",
        },
    }
    return {
        "rules": {
            "profiles": {
                "off": [],
                "dual-text": ["0,1"],
                "single-text": ["0", "1", "0,1"],
                "render-model": ["0", "1"],
                "motion-model": ["0", "1", "0,1"],
            },
            "runtime_names": {
                "text": "Text Runtime",
                "render-pool": "Render Runtime",
                "motion-pool": "Motion Runtime",
            },
            "workloads": workloads,
        },
        "services": [
            {
                "container": "fixture-render",
                "display_label": "Render",
                "group": "Workers",
                "title": "Render Worker",
                "url": "https://render.example.invalid/",
            }
        ],
    }


def synthetic_engine():
    catalogue = synthetic_catalogue()
    return {
        "host_proc": "/host/proc",
        "host_sys": "/host/sys",
        "lease_dir": "/run/ai-gpu",
        "socket": "/run/engine.sock",
        "state_dir": "/state",
        "workloads": {
            profile: {
                "placements": {
                    devices: {
                        "name": "fixture-" + item["runtime"],
                        "config": {
                            "Image": "fixture.invalid/workload:1.0.0",
                            "Cmd": ["true"],
                            "Labels": {"com.docker.compose.service": profile},
                            "Env": ["AI_GPU_DEVICES=" + devices],
                        },
                    }
                    for devices in catalogue["rules"]["profiles"][profile]
                }
            }
            for profile, item in catalogue["rules"]["workloads"].items()
        },
    }
