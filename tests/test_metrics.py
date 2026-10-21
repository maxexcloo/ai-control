"""Read telemetry only from supplied host filesystem fixtures."""

import tempfile
import unittest
from pathlib import Path

from control.metrics import gpu_metrics, host_metrics


class MetricsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / "host-proc"
        self.sys = self.root / "host-sys"
        self.state = self.root / "state"
        self.state.mkdir()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_host_metrics_use_supplied_mounts_and_host_process_network(self):
        self.write(self.proc / "uptime", "42.5 0\n")
        self.write(
            self.proc / "stat", "cpu 10 0 20 30 5 0 0 0\ncpu0 10 0 20 30 5 0 0 0\n"
        )
        self.write(self.proc / "cpuinfo", "model name : Fixture CPU\n")
        self.write(self.proc / "meminfo", "MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
        self.write(
            self.proc / "1/net/route",
            "Iface Destination Gateway Flags RefCnt Use Metric Mask\nfixture0 00000000 00000000 0003 0 0 10 00000000\n",
        )
        self.write(self.sys / "class/net/fixture0/statistics/rx_bytes", "123")
        self.write(self.sys / "class/net/fixture0/statistics/tx_bytes", "456")
        result = host_metrics(self.state, self.proc, self.sys)
        self.assertEqual(result["cpu"]["name"], "Fixture CPU")
        self.assertEqual(result["cpu"]["cores"], 1)
        self.assertEqual(result["cpu"]["idle_ticks"], 35)
        self.assertEqual(
            result["memory"],
            {"total_bytes": 1024000, "available_bytes": 409600, "used_bytes": 614400},
        )
        self.assertEqual(
            result["network"],
            {"interface": "fixture0", "received_bytes": 123, "sent_bytes": 456},
        )
        self.assertEqual(result["disk"]["path"], str(self.state))

    def test_gpu_ordinal_follows_kfd_and_uses_supplied_device_sensors(self):
        self.write(
            self.sys / "class/kfd/kfd/topology/nodes/0/properties", "simd_count 0\n"
        )
        self.write(
            self.sys / "class/kfd/kfd/topology/nodes/1/properties",
            "simd_count 32\ndrm_render_minor 129\n",
        )
        device = self.sys / "class/drm/renderD129/device"
        for filename, value in [
            ("gpu_busy_percent", "17"),
            ("mem_info_vram_total", "4096"),
            ("mem_info_vram_used", "1024"),
            ("vendor", "0x1002"),
            ("device", "0x1234"),
        ]:
            self.write(device / filename, value)
        self.write(device / "hwmon/hwmon0/temp1_label", "edge")
        self.write(device / "hwmon/hwmon0/temp1_input", "57000")
        pci = self.root / "pci.ids"
        self.write(pci, "1002  Fixture Vendor\n\t1234  Fixture GPU\n")
        result = gpu_metrics(self.sys, pci)
        self.assertEqual(set(result), {"0"})
        self.assertEqual(result["0"]["name"], "Fixture GPU")
        self.assertEqual(result["0"]["busy_percent"], 17)
        self.assertEqual(result["0"]["temperature_celsius"], 57)
        self.assertEqual(result["0"]["vram_used_bytes"], 1024)

    def test_missing_optional_telemetry_does_not_probe_ambient_host_paths(self):
        self.assertEqual(gpu_metrics(self.sys, self.root / "missing-pci.ids"), {})
        result = host_metrics(self.state, self.proc, self.sys)
        self.assertEqual(result["cpu"], {})
        self.assertEqual(result["memory"], {})
        self.assertEqual(result["network"], {})
        self.assertEqual(result["disk"]["path"], str(self.state))
