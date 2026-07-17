from __future__ import annotations

import os
from typing import Any

from .chat_bridge_gateway import ChatBridgeModelGateway
from .g3_runtime_gateway import G3AuditedModelGateway


class PortableModelGateway:
    """Select the configured model transport without changing workflow code.

    ``OPENAI_COMPATIBLE`` uses the normal audited HTTP gateway. ``CHAT_BRIDGE``
    writes the exact prompt envelope and schema to a durable local file bridge so
    another model (including an interactive ChatGPT session) can answer it.  The
    bridge is explicit rather than automatic: merely defining ``CHAT_BRIDGE_DIR``
    never changes production routing unless ``MODEL_GATEWAY_MODE=CHAT_BRIDGE``.
    """

    def __new__(cls, settings: Any, pack: Any):
        mode = str(
            getattr(settings, "model_gateway_mode", None)
            or os.getenv("MODEL_GATEWAY_MODE", "OPENAI_COMPATIBLE")
        ).strip().upper()
        if mode == "CHAT_BRIDGE":
            bridge_dir = str(
                getattr(settings, "chat_bridge_dir", None)
                or os.getenv("CHAT_BRIDGE_DIR", "")
            ).strip()
            if not bridge_dir:
                raise ValueError(
                    "MODEL_GATEWAY_MODE=CHAT_BRIDGE requires CHAT_BRIDGE_DIR"
                )
            return ChatBridgeModelGateway(settings, pack)
        if mode in {"OPENAI_COMPATIBLE", "AUDITED_HTTP", "DEFAULT"}:
            return G3AuditedModelGateway(settings, pack)
        raise ValueError(
            "MODEL_GATEWAY_MODE must be OPENAI_COMPATIBLE or CHAT_BRIDGE"
        )
