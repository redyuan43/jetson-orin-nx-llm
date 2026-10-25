"""Durable, load-only Comfy preloading under an original device-policy barrier.

No inference API is imported here.  The output node uses Comfy's bounded
ModelPatcher loading primitive; encoding, sampling and decoding are absent.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import urllib.request
import uuid

from .weight_preload_protocol import (
    build_preload_request, validate_config, validate_preload_request,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("preload_redirect_forbidden")


def request_json(base, path, body=None, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(base.rstrip("/") + path,
        data=None if body is None else canonical(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with opener.open(request, timeout=8) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("preload_response_limit")
    return json.loads(raw)


def activity(config):
    central = config["central"]
    from .weight_preload_protocol import private_token
    return request_json(central["base_url"], "/internal/v1/resource-groups/" +
        config["group_id"] + "/activity", headers={
            "Authorization": "Bearer " + private_token(central["token_file"])})


def available_ram():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise ValueError("preload_ram_observation_missing")


def check_ram(config, ram=None):
    if (available_ram() if ram is None else ram) < config["budget"]["min_ram_available_bytes"]:
        raise ValueError("preload_host_memory_budget_unavailable")


def consume_ticket(config, claims):
    """Native /prompt has no UUID deduplication; spend each ticket only once."""
    path = Path(config["ticket_key_file"]).parent / "weight-preload-tickets.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite3.connect(path, timeout=5) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS tickets (operation TEXT PRIMARY KEY, prompt TEXT UNIQUE, nonce TEXT UNIQUE)")
        try:
            db.execute("INSERT INTO tickets VALUES (?,?,?)",
                (claims["operation_id"], claims["prompt_id"], claims["nonce"]))
        except sqlite3.IntegrityError:
            raise ValueError("preload_ticket_already_spent") from None


class WeightPreloadClient:
    """Exactly one POST attempt per journal; a missing receipt is never replayed."""
    def __init__(self, config, journal_path, *, transport=request_json):
        self.config = validate_config(config)
        self.journal_path = Path(journal_path)
        self.transport = transport

    def ensure(self, demand):
        self.journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(str(self.journal_path) + ".lock", "a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return self._ensure(demand)

    def _ensure(self, demand):
        operation = demand["operation_id"]
        if self.journal_path.exists():
            row = json.loads(self.journal_path.read_text())
            if row["operation_id"] != operation:
                raise ValueError("preload_journal_operation_conflict")
        else:
            row = {"schema_version": 1, "operation_id": operation,
                "prompt_id": str(uuid.uuid4()), "nonce": secrets.token_hex(32),
                "state": "prepared", "created_at": time.time()}
            row["request"] = build_preload_request(self.config, demand, row["nonce"], row["prompt_id"])
            atomic_json(self.journal_path, row)
        if row["state"] in {"failed", "complete"}:
            return {key: value for key, value in row.items() if key != "request"}
        base = self.config["native_base"]
        if row["state"] == "prepared":
            # Persist uncertainty BEFORE crossing the network.  A crash here
            # deliberately fails closed, rather than possibly loading twice.
            row["state"] = "unknown"
            atomic_json(self.journal_path, row)
            try:
                answer = self.transport(base, "/prompt", row["request"], {
                    "X-Fleet-Operation": operation,
                    "X-Fleet-Executor": "device-policy:" + self.config["group_id"]})
                if answer.get("prompt_id") != row["prompt_id"]:
                    raise ValueError("preload_receipt_prompt_mismatch")
                row["state"] = "running"
                atomic_json(self.journal_path, row)
            except Exception:
                pass
        try:
            history = self.transport(base, "/history/" + row["prompt_id"])
            result = history.get(row["prompt_id"])
            if result is not None:
                saved_prompt = result.get("prompt", [])
                if len(saved_prompt) < 3 or saved_prompt[1] != row["prompt_id"] or saved_prompt[2] != row["request"]["prompt"]:
                    raise ValueError("preload_history_identity_mismatch")
                status = result.get("status", {})
                receipts = result.get("outputs", {}).get("preload", {}).get("siyuan_weight_preload", [])
                if status.get("status_str") == "error":
                    row["state"] = "failed"
                    row["reason"] = "preload_execution_failed"
                elif status.get("completed") is True and status.get("status_str") == "success":
                    if (len(receipts) != 1 or receipts[0].get("operation_id") != operation
                            or receipts[0].get("prompt_id") != row["prompt_id"]
                            or receipts[0].get("graph_sha256") != self.config["graph_sha256"]
                            or receipts[0].get("model_class") != self.config["model_class"]
                            or receipts[0].get("scope") != "main_model_partial_cuda"
                            or receipts[0].get("inference_executed") is not False):
                        raise ValueError("preload_load_receipt_missing")
                    hook = self.transport(base, "/siyuan/loaded-models")
                    valid = [model for model in hook.get("models", [])
                        if model.get("model_class") == self.config["model_class"]
                        and model.get("device_kind") == "cuda"
                        and type(model.get("loaded_bytes")) is int
                        and self.config["budget"]["min_loaded_bytes"] <= model["loaded_bytes"]
                            <= self.config["budget"]["max_cuda_bytes"]]
                    if not valid or hook.get("busy") is not False:
                        raise ValueError("preload_residency_not_confirmed")
                    row.update(state="complete", receipt=receipts[0], residency=hook, completed_at=time.time())
                atomic_json(self.journal_path, row)
            else:
                queue = self.transport(base, "/queue")
                present = any(len(item) > 1 and item[1] == row["prompt_id"] for key in
                    ("queue_running", "queue_pending") for item in queue.get(key, []))
                row["state"] = "running" if present else "unknown"
                atomic_json(self.journal_path, row)
        except Exception as exc:
            row["reason"] = type(exc).__name__
        return {key: value for key, value in row.items() if key != "request"}


def load_bounded(model, config, management, *, ram=None):
    """Use the same primitive as load_models_gpu, with an explicit positive cap.

    The normal Comfy allocator can use 40% of available memory and --novram
    overrides its budget.  Neither global mode nor ordinary inference changes.
    """
    budget = config["budget"]
    remaining_ram = available_ram() if ram is None else ram
    host_reserve = budget["reserve_cuda_bytes"] if budget["unified_memory"] else 2 * 1024**3
    if remaining_ram < host_reserve:
        raise ValueError("preload_host_reserve_unavailable")
    if type(model.model).__name__ != config["model_class"] or model.load_device.type != "cuda":
        raise ValueError("preload_model_identity_mismatch")
    entries = management.current_loaded_models
    matching = [entry for entry in entries if entry.model is model]
    if any(entry.model is not model for entry in entries):
        raise ValueError("preload_other_model_resident")
    if len(matching) > 1:
        raise ValueError("preload_duplicate_model_entry")
    loaded = matching[0] if matching else management.LoadedModel(model)
    before = loaded.model_loaded_memory()
    free = int(management.get_free_memory(model.load_device))
    cap = min(budget["max_cuda_bytes"], free + before - budget["reserve_cuda_bytes"])
    if cap < budget["min_loaded_bytes"]:
        raise ValueError("preload_cuda_budget_unavailable")
    if before > cap:
        raise ValueError("preload_existing_residency_exceeds_budget")
    try:
        if before < budget["min_loaded_bytes"]:
            loaded.model_load(lowvram_model_memory=cap - before, force_patch_weights=False)
        actual = loaded.model_loaded_memory()
        if not budget["min_loaded_bytes"] <= actual <= cap:
            raise ValueError("preload_loaded_bytes_outside_budget")
        if int(management.get_free_memory(model.load_device)) < budget["reserve_cuda_bytes"]:
            raise ValueError("preload_cuda_reserve_exhausted")
        if not matching:
            entries.insert(0, loaded)
        return {"model_class": config["model_class"], "device_kind": "cuda",
            "loaded_bytes": actual, "model_bytes": loaded.model_memory(),
            "offloaded_bytes": max(0, loaded.model_memory() - actual),
            "max_cuda_bytes": budget["max_cuda_bytes"],
            "reserve_cuda_bytes": budget["reserve_cuda_bytes"],
            "scope": "main_model_partial_cuda", "inference_executed": False}
    except Exception:
        if not matching:
            try:
                loaded.model_unload()
            except Exception:
                # model_load may fail before Comfy installs its finalizer.
                model.detach()
        raise


def execute_preload(model, authorization_json, config):
    config = validate_config(config)
    graph = json.loads(Path(config["graph_file"]).read_text())
    graph["preload"]["inputs"]["authorization_json"] = authorization_json
    authorization = json.loads(authorization_json)
    claims = authorization["claims"]
    body = {"prompt": graph, "prompt_id": claims["prompt_id"],
        "extra_data": {"siyuan_weight_preload": 1}}
    validate_preload_request(config, body, {
        "X-Fleet-Operation": claims["operation_id"],
        "X-Fleet-Executor": claims["executor_id"]}, activity(config))
    from server import PromptServer
    running, pending = PromptServer.instance.prompt_queue.get_current_queue()
    if pending or len(running) != 1 or running[0][1] != claims["prompt_id"]:
        raise ValueError("preload_queue_identity_not_exclusive")
    from comfy import model_management
    receipt = load_bounded(model, config, model_management)
    receipt.update(operation_id=claims["operation_id"], prompt_id=claims["prompt_id"],
        graph_sha256=config["graph_sha256"], observed_at=time.time())
    return {"ui": {"siyuan_weight_preload": [receipt]}}
