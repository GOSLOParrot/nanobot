"""GOSLO dual-body mode middleware.

When GOSLO's Live body (Brain Agent) is active, this hook intercepts incoming
messages on the Chat body and forwards them to the Brain via Redis instead of
processing them locally.  When Live is offline, messages pass through normally.

Usage (in start_goslo_chat.py or after ChannelManager init):
    from nanobot.channels.goslo_mode import install_mode_hook
    install_mode_hook(channel_manager, redis_url="redis://localhost:6379/0")
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

_HASH_GOSLO_MODE = "parrot.goslo.mode"
_CH_EXTERNAL_COMMANDS = "parrot.external.commands"


async def goslo_mode_hook(
    *,
    channel: Any,
    sender_id: str,
    chat_id: str,
    content: str,
    media: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    _redis_url: str = "",
) -> bool | None:
    """Pre-handle hook: check GOSLO mode and decide whether to process locally.

    Returns False to suppress normal processing (Live mode → forward to Brain).
    Returns None to continue with normal agent processing (Chat mode).
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return None

    r: aioredis.Redis | None = getattr(goslo_mode_hook, "_redis", None)
    if r is None:
        url = _redis_url or "redis://localhost:6379/0"
        r = aioredis.from_url(url, decode_responses=True)
        goslo_mode_hook._redis = r  # type: ignore[attr-defined]

    try:
        mode_data = await r.hgetall(_HASH_GOSLO_MODE)
    except Exception:
        logger.warning("goslo_mode: Redis unreachable, defaulting to chat mode")
        return None

    active_body = mode_data.get("active_body", "chat")

    if active_body == "live":
        logger.info(
            "goslo_mode: Live body active, forwarding message to Brain (from=%s)",
            sender_id,
        )
        try:
            payload = json.dumps({
                "source": "goslo_chat",
                "sender_id": sender_id,
                "chat_id": chat_id,
                "content": content,
                "channel": getattr(channel, "name", "unknown"),
            })
            await r.publish(_CH_EXTERNAL_COMMANDS, payload)
        except Exception:
            logger.exception("goslo_mode: failed to forward to Brain")

        from nanobot.bus.events import OutboundMessage
        await channel.bus.publish_outbound(OutboundMessage(
            channel=channel.name,
            chat_id=chat_id,
            content="我现在在 Live 模式哦~ 消息已经转给 AR 那边的我了 desuwa！",
        ))
        return False

    return None


def install_mode_hook(channel_manager: Any, redis_url: str = "") -> int:
    """Install the mode hook on all non-parrot_bus channels in the manager.

    Returns the number of channels hooked.
    """
    count = 0
    for name, ch in channel_manager.channels.items():
        if name == "parrot_bus":
            continue
        ch.pre_handle_hook = _make_hook(redis_url)
        logger.info("goslo_mode: hook installed on '{}' channel", name)
        count += 1
    return count


def _make_hook(redis_url: str):
    """Create a closure that binds the redis_url."""
    async def hook(**kwargs):
        return await goslo_mode_hook(**kwargs, _redis_url=redis_url)
    return hook
