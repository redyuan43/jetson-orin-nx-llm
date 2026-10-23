"""Cross-component CPU contract for the deployed NX5 Flint text path."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from aiohttp.test_utils import make_mocked_request

from test_nx5_service import module


FLEET_CONTROL = os.environ.get("NX5_FLEET_CONTROL_ROOT")


@unittest.skipUnless(FLEET_CONTROL, "set NX5_FLEET_CONTROL_ROOT to the reviewed media fleet-control source")
class ConcurrentFlintWaiterTests(unittest.TestCase):
    def setUp(self):
        root = str(Path(FLEET_CONTROL).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        self.admission = importlib.import_module("fleet_control.text_admission")
        self.ingress = importlib.import_module("fleet_control.text_ingress")

    def test_original_flint_barrier_accepts_other_pure_waiting_request(self):
        class Wire:
            def __init__(self):
                self.rows = {}

            def reserve(self, request):
                op = request["operation_id"]
                status = "preparing" if not self.rows else "waiting"
                row = request["candidates"][0]
                receipt = {"executor_id": "nx5-text", "operation_id": op,
                           "request_digest": self.journal._document(op)["request_digest"],
                           "backend_id": row["backend_id"], "status": status}
                self.rows[op] = {**receipt, "resources": row["resources"]}
                return receipt

            def claim(self, request):
                return dict(self.rows[request["operation_id"]])

            def group_activity(self, group):
                raise AssertionError("the admission handler activity endpoint must not poll central")

        profile = {"profile_id": "nx5-flint", "profile_digest": "a" * 64,
                   "model_alias": "flint", "native_model": "flint-native",
                   "mode": "flint", "group_id": "nx5-igpu-group",
                   "resources": ["nx5-igpu"], "group_resources": ["nx5-igpu"],
                   "qualified": True, "published_context": 24576,
                   "request_modes": ["chat.completions"]}
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "release.json").write_text(json.dumps({"files": {}, "release": "cpu",
                "commit": "cpu", "source_commit": "cpu"}))
            journal = self.admission.TextJournal(Path(tmp) / "gate.sqlite3", profile, wire,
                                                 executor_id="nx5-text", queue_limit=16)
            wire.journal = journal
            try:
                operations = ["op_" + "1" * 32, "op_" + "2" * 32]
                for op in operations:
                    journal.begin(op, {"model": "flint", "messages": [{"role": "user", "content": "2+2"}]})
                    journal.advance(op, "ready")

                pending = [dict(wire.rows[op]) for op in operations]
                self.assertEqual([row["status"] for row in pending], ["preparing", "waiting"])
                self.assertEqual(journal.activity()["waiting"], 2)

                config = {"enabled": True, "profile": profile, "raw_url": "http://127.0.0.1:19991",
                          "client_authorization": "Bearer client-fixture",
                          "controller_authorization": "Bearer controller-fixture",
                          "native_authorization": "Bearer native-fixture"}
                app = self.ingress.create_app(config, journal)
                gate = next(cb.__self__ for cb in app.on_startup if cb.__name__ == "start")
                observation = asyncio.run(gate.activity(make_mocked_request("GET", "/internal/activity")))
                gate_activity = json.loads(observation.text)
                # This is the one runtime-only lifecycle field. The fixed profile was
                # already publicly promoted before this test; TextGate returns the
                # other fields directly from its real TextJournal handler.
                gate_activity["owner_admission"] = True
                self.assertEqual(gate_activity["waiting"], 2)

                with patch.object(module, "__file__", str(Path(tmp) / "scripts/nx5/service.py")):
                    service = module.Service({"root": tmp, "state": str(Path(tmp) / "owner"),
                                              "managed_comfy": True})
                mode = service.mode
                transition = {"group_id": "nx5-igpu-group", "resources": ["nx5-igpu"],
                              "executor_id": "nx5-text", "operation_id": operations[0],
                              "target_mode": "flint", "lease_barrier": True}
                central = {"group_id": "nx5-igpu-group", "resources": ["nx5-igpu"],
                           "mode": "off", "state": "ready", "active": 0, "fresh": True,
                           "leases": [], "pending": pending, "transition": transition,
                           "policy_demand": None}
                service.resident.record = {"operation_id": "op_" + "a" * 32,
                    "backend_id": "nx5-ssd-minicpm", "mode": "llm", "status": "released"}
                mode.policy_profiles = {"flint": {"profile_id": "nx5-flint", "version": "a" * 64}}
                mode.activity = Mock(return_value=central)
                mode.transition = Mock(return_value=transition)
                mode.unit = Mock(return_value="inactive")
                mode.flint_unit = Mock(side_effect=["inactive", "active"])
                mode.flint_gate_unit = Mock(return_value="active")
                mode.flint_gate_activity = Mock(return_value=gate_activity)
                mode.wait_flint_ready = Mock(return_value={"ready": True, "active": 0, "slots": 1})
                mode.queue = Mock(return_value=0)
                mode.flint_action = Mock()
                mode.action = Mock()
                mode.report = Mock()

                self.assertEqual(service.resident.record["operation_id"], "op_" + "a" * 32)
                self.assertNotEqual(service.resident.record["operation_id"], transition["operation_id"])
                self.assertTrue(mode.own_flint_transition(transition, central, gate_activity))
                self.assertFalse(mode.own_flint_transition(
                    transition, central, dict(gate_activity, active=1)))
                self.assertFalse(mode.own_flint_transition(
                    transition, central, dict(gate_activity, unknown=1)))
                submitting = dict(central, pending=[pending[0], dict(pending[1], status="submitting")])
                self.assertFalse(mode.own_flint_transition(transition, submitting, gate_activity))

                with patch("resident.require_idle"):
                    mode.tick()

                mode.flint_action.assert_called_once_with("start")
                mode.action.assert_not_called()
                self.assertEqual(mode.report.call_args.args[0],
                                 {"mode": "flint", "state": "ready", "active": 0})
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
