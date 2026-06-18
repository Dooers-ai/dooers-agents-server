from dooers.agents.server.broadcast import BroadcastManager
from dooers.agents.server.config import AgentConfig, OnSettingsUpdated
from dooers.agents.server.dispatch import DispatchStream
from dooers.agents.server.exceptions import DispatchError, HandlerError, UnsupportedContentTypeError
from dooers.agents.server.features.analytics import (
    AgentAnalytics,
    AnalyticsBatch,
    AnalyticsCollector,
    AnalyticsEvent,
    AnalyticsEventPayload,
)
from dooers.agents.server.features.channels.whatsapp import (
    WhatsappOutboundCallback,
    create_dooers_whatsapp_outbound,
    dooers_whatsapp_hmac_key_fingerprint,
    normalize_e164,
    parse_dooers_whatsapp_instance_hmac_map,
    tools_base_url,
    verify_dooers_whatsapp_tool_inbound_signature,
    verify_dooers_whatsapp_tool_inbound_with_persistence,
    whatsapp_thread_id,
)
from dooers.agents.server.features.settings import (
    AgentSettings,
    SettingsBroadcaster,
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsFieldVisibility,
    SettingsSchema,
    SettingsSelectOption,
)
from dooers.agents.server.handlers.content_policy import normalize_allowed_content_types
from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.handlers.incoming import AgentIncoming
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.handlers.pipeline import Handler, UploadReferenceError
from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.llm import format_user_input
from dooers.agents.server.persistence.base import Persistence
from dooers.agents.server.protocol.models import (
    Actor,
    AudioPart,
    ContactPart,
    ContentPart,
    DocumentPart,
    EventType,
    ImagePart,
    Run,
    RunStatus,
    TextPart,
    Thread,
    ThreadEvent,
    User,
    WireC2S_AudioPart,
    WireC2S_ContactPart,
    WireC2S_ContentPart,
    WireC2S_DocumentPart,
    WireC2S_ImagePart,
    WireC2S_TextPart,
    WireS2C_AudioPart,
    WireS2C_ContactPart,
    WireS2C_ContentPart,
    WireS2C_DocumentPart,
    WireS2C_ImagePart,
    WireS2C_TextPart,
)
from dooers.agents.server.registry import ConnectionRegistry
from dooers.agents.server.repository import Repository
from dooers.agents.server.server import AgentServer

__all__ = [
    # Core
    "AgentConfig",
    "OnSettingsUpdated",
    "AgentServer",
    "AgentContext",
    "AgentIncoming",
    "AgentSend",
    "AgentMemory",
    "ConnectionRegistry",
    "BroadcastManager",
    "Persistence",
    # Dispatch
    "DispatchStream",
    "DispatchError",
    "HandlerError",
    "UnsupportedContentTypeError",
    "UploadReferenceError",
    "normalize_allowed_content_types",
    "Handler",
    "format_user_input",
    # Repository
    "Repository",
    # Protocol models
    "User",
    "ContentPart",
    "TextPart",
    "AudioPart",
    "ContactPart",
    "ImagePart",
    "DocumentPart",
    "WireC2S_ContentPart",
    "WireC2S_TextPart",
    "WireC2S_AudioPart",
    "WireC2S_ContactPart",
    "WireC2S_ImagePart",
    "WireC2S_DocumentPart",
    "WireS2C_ContentPart",
    "WireS2C_TextPart",
    "WireS2C_AudioPart",
    "WireS2C_ContactPart",
    "WireS2C_ImagePart",
    "WireS2C_DocumentPart",
    "Thread",
    "ThreadEvent",
    "Run",
    "RunStatus",
    "Actor",
    "EventType",
    # Analytics
    "AnalyticsEvent",
    "AnalyticsEventPayload",
    "AnalyticsBatch",
    "AnalyticsCollector",
    "AgentAnalytics",
    # Settings
    "SettingsFieldType",
    "SettingsFieldVisibility",
    "SettingsField",
    "SettingsFieldGroup",
    "SettingsSelectOption",
    "SettingsSchema",
    "SettingsBroadcaster",
    "AgentSettings",
    # WhatsApp (Dooers tools channel)
    "WhatsappOutboundCallback",
    "create_dooers_whatsapp_outbound",
    "dooers_whatsapp_hmac_key_fingerprint",
    "normalize_e164",
    "parse_dooers_whatsapp_instance_hmac_map",
    "tools_base_url",
    "verify_dooers_whatsapp_tool_inbound_signature",
    "verify_dooers_whatsapp_tool_inbound_with_persistence",
    "whatsapp_thread_id",
]
