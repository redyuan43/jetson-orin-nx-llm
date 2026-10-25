"""Signed, load-only output node. Never samples, encodes or decodes."""
import asyncio
import json
import os
from pathlib import Path

from fleet_control.comfy_preload import activity, check_ram, consume_ticket, execute_preload
from fleet_control.weight_preload_protocol import validate_config, validate_preload_request
from server import PromptServer

CONFIG = validate_config(json.loads(Path(os.environ["SIYUAN_WEIGHT_PRELOAD_CONFIG"]).read_text()))


class SiyuanPreloadModel:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",), "authorization_json": ("STRING", {})}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "preload"
    OUTPUT_NODE = True
    CATEGORY = "SIYUAN/lifecycle"

    def preload(self, model, authorization_json):
        return execute_preload(model, authorization_json, CONFIG)


def install_preflight():
    """Check the budget before Comfy's MODEL ancestors can allocate host RAM."""
    from aiohttp import web

    @web.middleware
    async def preflight(request, handler):
        if request.method == "POST" and request.path in {"/prompt", "/api/prompt"}:
            body = await request.json()
            nodes = body.get("prompt", {}) if isinstance(body, dict) else {}
            if any(isinstance(node, dict) and node.get("class_type") == "SiyuanPreloadModel"
                   for node in nodes.values()):
                try:
                    row = await asyncio.to_thread(activity, CONFIG)
                    claims = validate_preload_request(CONFIG, body, request.headers, row)
                    check_ram(CONFIG)
                    queue = PromptServer.instance.prompt_queue
                    running, pending = queue.get_current_queue()
                    if running or pending or row.get("active") != 0:
                        raise ValueError("preload_start_requires_idle")
                    consume_ticket(CONFIG, claims)
                except Exception:
                    return web.json_response({"error": "weight_preload_preflight_rejected"}, status=409)
        return await handler(request)
    PromptServer.instance.app.middlewares.append(preflight)


install_preflight()


async def preload_capability(request):
    from aiohttp import web
    return web.json_response({"schema_version": 1, "group_id": CONFIG["group_id"],
        "profile_id": CONFIG["profile_id"], "profile_version": CONFIG["profile_version"],
        "graph_sha256": CONFIG["graph_sha256"], "model_class": CONFIG["model_class"],
        "budget": CONFIG["budget"], "scope": "main_model_partial_cuda",
        "inference_allowed": False})


PromptServer.instance.routes.get("/siyuan/weight-preload")(preload_capability)
NODE_CLASS_MAPPINGS = {"SiyuanPreloadModel": SiyuanPreloadModel}
NODE_DISPLAY_NAME_MAPPINGS = {"SiyuanPreloadModel": "SIYUAN 主模型分层预载"}
