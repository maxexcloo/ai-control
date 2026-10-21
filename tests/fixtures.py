"""Synthetic deployment data: no host inventory or model catalogue is imported."""


def synthetic_catalogue():
    workloads = {
        'dual-text': {'kind': 'text', 'label': 'Dual Text', 'runtime': 'text'},
        'single-text': {'kind': 'text', 'label': 'Single Text', 'runtime': 'text'},
        'render-model': {'kind': 'image', 'label': 'Render Model', 'runtime': 'render-pool'},
        'motion-model': {'kind': 'video', 'label': 'Motion Model', 'runtime': 'motion-pool'},
    }
    return {
        'media': [{'id': 'off', 'label': 'Off'}, {'id': 'image', 'label': 'Image'}, {'id': 'pair', 'label': 'Image & Video'}],
        'models': [{'id': 'off', 'label': 'Off'}, {'id': 'dual-text', 'label': 'Dual Text'}, {'id': 'single-text', 'label': 'Single Text'}],
        'rules': {
            'media': {'image': ['0', '1'], 'pair': ['0,1'], 'off': []},
            'profiles': {'off': [], 'dual-text': ['0,1'], 'single-text': ['0', '1', '0,1'], 'render-model': ['0', '1'], 'motion-model': ['0', '1', '0,1']},
            'roles': {'image': 'render-model', 'video': 'motion-model'},
            'runtime_names': {'text': 'Text Runtime', 'render-pool': 'Render Runtime', 'motion-pool': 'Motion Runtime'},
            'workloads': workloads,
        },
        'services': [{'container': 'fixture-render', 'display_label': 'Render', 'group': 'Workers', 'title': 'Render Worker', 'url': 'https://render.example.invalid/'}],
    }
