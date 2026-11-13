"""Optional dual-IM channels; importing this package needs no provider SDK."""
from .gating import ChannelGate, ChatEnvelope, GatedDecision
from .dispatcher import ChannelAdapter, MultiOutboxDispatcher
from .feishu import FeishuChannelAdapter, FeishuIngress, build_lark_ws_dispatcher
from .dingtalk import DingTalkChannelAdapter, DingTalkChatIngress, DingTalkCardIngress, register_dingtalk_stream_handlers

__all__ = ["ChannelGate", "ChatEnvelope", "GatedDecision", "ChannelAdapter", "MultiOutboxDispatcher",
           "FeishuChannelAdapter", "FeishuIngress", "DingTalkChannelAdapter", "DingTalkChatIngress",
           "DingTalkCardIngress", "build_lark_ws_dispatcher", "register_dingtalk_stream_handlers"]
