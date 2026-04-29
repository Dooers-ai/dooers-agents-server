from dooers.broadcast import BroadcastManager
from dooers.config import AgentConfig, OnSettingsUpdated
from dooers.dispatch import DispatchStream
from dooers.exceptions import DispatchError, HandlerError, UnsupportedContentTypeError
from dooers.features.analytics import (
    AgentAnalytics,
    AnalyticsBatch,
    AnalyticsCollector,
    AnalyticsEvent,
    AnalyticsEventPayload,
)
from dooers.features.channels.whatsapp import (
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
from dooers.features.settings import (
    AgentSettings,
    SettingsBroadcaster,
    SettingsField,
    SettingsFieldGroup,
    SettingsFieldType,
    SettingsFieldVisibility,
    SettingsSchema,
    SettingsSelectOption,
)
from dooers.handlers.content_policy import normalize_allowed_content_types
from dooers.handlers.context import AgentContext
from dooers.handlers.incoming import AgentIncoming
from dooers.handlers.memory import AgentMemory
from dooers.handlers.pipeline import Handler, UploadReferenceError
from dooers.handlers.send import AgentSend
from dooers.llm import format_user_input
from dooers.persistence.base import Persistence
from dooers.protocol.models import (
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
from dooers.registry import ConnectionRegistry
from dooers.repository import Repository
from dooers.server import AgentServer

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
