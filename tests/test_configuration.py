"""Exercise supplied configuration and runtime-independent planning."""

import unittest

from fixtures import synthetic_catalogue

from control.allocation import allocation_plan, normalise_allocation
from control.server import validate_catalogue


class ConfigurationTests(unittest.TestCase):
    def test_missing_required_rules_fail_with_their_names(self):
        for key in ["profiles", "runtime_names", "workloads"]:
            catalogue = synthetic_catalogue()
            del catalogue["rules"][key]
            with self.assertRaisesRegex(ValueError, "catalogue.rules." + key):
                validate_catalogue(catalogue)

    def test_profile_and_runtime_identifiers_are_safe_before_engine_dispatch(self):
        for key, value in [("profile", "model;touch"), ("runtime", "runtime --option")]:
            catalogue = synthetic_catalogue()
            if key == "profile":
                catalogue["rules"]["workloads"][value] = catalogue["rules"][
                    "workloads"
                ].pop("single-text")
            else:
                catalogue["rules"]["workloads"]["single-text"]["runtime"] = value
            with self.assertRaises(ValueError):
                validate_catalogue(catalogue)

    def test_arbitrary_runtime_names_are_resolved_only_from_passed_rules(self):
        catalogue = synthetic_catalogue()
        rules = catalogue["rules"]
        rules["runtime_names"]["language-pool"] = rules["runtime_names"].pop("text")
        for workload in rules["workloads"].values():
            if workload["kind"] == "text":
                workload["runtime"] = "language-pool"
        validate_catalogue(catalogue)
        allocation = [{"profile": "single-text", "devices": ["1"]}]
        self.assertEqual(normalise_allocation(allocation, rules), allocation)
        result = allocation_plan(allocation, {"runtimes": {}}, rules=rules)
        self.assertEqual(
            result["start"],
            [{"profile": "single-text", "devices": ["1"], "runtime": "language-pool"}],
        )
        result = allocation_plan(
            [], {"runtimes": {"language-pool": {"running": True}}}, rules=rules
        )
        self.assertEqual(result["stop"], ["language-pool"])

    def test_service_presentation_is_required_from_configuration(self):
        for key in ["display_label", "group", "title", "url"]:
            catalogue = synthetic_catalogue()
            del catalogue["services"][0][key]
            with self.assertRaisesRegex(ValueError, "catalogue.services"):
                validate_catalogue(catalogue)
