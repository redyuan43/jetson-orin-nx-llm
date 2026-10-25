"""Read-only ComfyUI model residency endpoint for the NX4 local runtime."""
from __future__ import annotations

import importlib
import ipaddress
import re
import time

from server import PromptServer

SOURCE = "comfy.current_loaded_models"
MAX_MODELS = 128
_MAX_BYTES = (1 << 63) - 1
_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class ResidencyUnknown(Exception):
    """The live ComfyUI residency snapshot cannot be trusted."""


def _is_loopback(request) -> bool:
    transport = getattr(request, "transport", None)
    get_extra_info = getattr(transport, "get_extra_info", None)
    if not callable(get_extra_info):
        return False
    peer = get_extra_info("peername")
    if not isinstance(peer, (tuple, list)) or not peer or not isinstance(peer[0], str):
        return False
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _queue_checkpoint(prompt_server):
    queue = getattr(prompt_server, "prompt_queue", None)
    running = getattr(queue, "currently_running", None)
    task_counter = getattr(queue, "task_counter", None)
    if not isinstance(running, dict) or isinstance(task_counter, bool) or not isinstance(task_counter, int):
        raise ResidencyUnknown
    return id(queue), task_counter, bool(running)


def _capture(model_management):
    loaded = getattr(model_management, "current_loaded_models", None)
    if not isinstance(loaded, (list, tuple)) or len(loaded) > MAX_MODELS:
        raise ResidencyUnknown

    snapshot = []
    rows = []
    for entry in tuple(loaded):
        patcher = getattr(entry, "model", None)
        if patcher is None:
            raise ResidencyUnknown
        model = getattr(patcher, "model", None)
        if model is None:
            raise ResidencyUnknown
        model_class = type(model).__name__
        patcher_class = type(patcher).__name__
        if not _CLASS_NAME.fullmatch(model_class) or not _CLASS_NAME.fullmatch(patcher_class):
            raise ResidencyUnknown

        device = getattr(entry, "device", None)
        device_kind = getattr(device, "type", None)
        if device_kind not in {"cpu", "cuda"}:
            raise ResidencyUnknown

        loaded_memory = getattr(entry, "model_loaded_memory", None)
        if not callable(loaded_memory):
            raise ResidencyUnknown
        size = loaded_memory()
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_BYTES:
            raise ResidencyUnknown

        snapshot.append((id(entry), id(patcher), model_class, patcher_class, device_kind, size))
        rows.append({"model_class": model_class, "device_kind": device_kind, "loaded_bytes": size})

    return tuple(snapshot), rows


async def loaded_models(request):
    from aiohttp import web

    if not _is_loopback(request):
        return web.json_response({"error": "loopback_only"}, status=403)

    try:
        prompt_server = PromptServer.instance
        before = _queue_checkpoint(prompt_server)
        model_management = importlib.import_module("comfy.model_management")
        first, _ = _capture(model_management)
        second, models = _capture(model_management)
        after = _queue_checkpoint(prompt_server)
        if first != second or before[:2] != after[:2]:
            raise ResidencyUnknown
        loaded_bytes = sum(row["loaded_bytes"] for row in models)
        if loaded_bytes > _MAX_BYTES:
            raise ResidencyUnknown
    except Exception:
        return web.json_response({"error": "residency_unknown"}, status=503)

    return web.json_response({
        "schema_version": 1,
        "observed_at": time.time(),
        "source": SOURCE,
        "models": models,
        "count": len(models),
        "loaded_bytes": loaded_bytes,
        "model_loaded": loaded_bytes > 0,
        "busy": after[2],
    })


PromptServer.instance.routes.get("/siyuan/loaded-models")(loaded_models)

# ComfyUI's custom-node loader accepts server-only packages with no node classes.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
