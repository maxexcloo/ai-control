"""Shared workload identity and manual GPU placement rules."""
import json
import os
from pathlib import Path

WORKLOADS = json.loads((Path(os.environ['AI_ROOT']) / 'workloads.json').read_text())
PROFILES = {profile: (item['runtime'], item['devices_variable']) for profile, item in WORKLOADS.items()}
CONTAINERS = {item['runtime']: item['container'] for item in WORKLOADS.values()}
GPU_RULES = {
    'media': {'image': ['0', '1'], 'pair': ['0,1'], 'off': []},
    'roles': {'image': 'comfyui-image', 'video': 'comfyui-video'},
    'profiles': {'off': [], **{profile: item['placements'] for profile, item in WORKLOADS.items()}},
    'runtime_names': {item['runtime']: item['label'] for item in reversed(list(WORKLOADS.values()))},
    'workloads': WORKLOADS,
}


def runtime(profile):
    return WORKLOADS[profile]['runtime']


def catalogue():
    models = [{'id': profile, 'label': item['model_label']}
              for profile, item in WORKLOADS.items() if item['kind'] == 'text']
    return {
        'media': [{'id': 'off', 'label': 'Off'}, {'id': 'image', 'label': WORKLOADS['comfyui-image']['label']},
                  {'id': 'pair', 'label': 'ComfyUI Image & Video'}],
        'models': [{'id': 'off', 'label': 'Off'}, *models],
        'rules': GPU_RULES,
    }
