"""Admissão Dooers Threads opt-in; nenhum handler, broker ou cliente é ativado por import."""

from .adapter import (
    DooersThreadsAdmission,
    DooersThreadsAdmissionError,
    DooersThreadsConfirmedResult,
    DooersThreadsCurrentLease,
    DooersThreadsDeliveryAdapter,
    DooersThreadsExecutor,
    DooersThreadsLeaseGate,
)
from .artifacts import DooersThreadsArtifactsClient, DooersThreadsHydratedArtifact
from .callbacks import DooersThreadsCallbackClient, DooersThreadsCallbackError, DooersThreadsHeartbeatGate
from .config import DooersThreadsConfig, DooersThreadsConfigurationError, DooersThreadsCredentialRequest, DooersThreadsInstanceCredential
from .remote import DooersThreadsRemoteTransport, cloud_run_transport
from .remote_credentials import remote_core_credentials

__all__ = [
    "DooersThreadsRemoteTransport",
    "cloud_run_transport",
    "remote_core_credentials",
    "DooersThreadsAdmission",
    "DooersThreadsAdmissionError",
    "DooersThreadsConfig",
    "DooersThreadsConfigurationError",
    "DooersThreadsConfirmedResult",
    "DooersThreadsCredentialRequest",
    "DooersThreadsCurrentLease",
    "DooersThreadsDeliveryAdapter",
    "DooersThreadsExecutor",
    "DooersThreadsInstanceCredential",
    "DooersThreadsLeaseGate",
]

from .client import DooersThreadsContextClient, DooersThreadsContextError
from .context import (
    DooersThreadsCancellation,
    DooersThreadsExecutionContext,
    DooersThreadsHandlerInput,
    DooersThreadsPrivateResolver,
    DooersThreadsPrivateResources,
    DooersThreadsPrivateScope,
    current_dooers_threads_context,
    dooers_threads_handler_context,
)
from .group import AgentDirectory, DooersThreadsGroupTurn, decide_group_turn
from .memory import DooersThreadsArtifactReference, DooersThreadsMemory, DooersThreadsThreadView
from .window import DooersThreadsHistoryLimitError

__all__ += [
    "DooersThreadsCancellation",
    "DooersThreadsContextClient",
    "DooersThreadsContextError",
    "DooersThreadsExecutionContext",
    "DooersThreadsHandlerInput",
    "DooersThreadsHistoryLimitError",
    "DooersThreadsPrivateResolver",
    "DooersThreadsPrivateResources",
    "DooersThreadsPrivateScope",
    "current_dooers_threads_context",
    "dooers_threads_handler_context",
    "DooersThreadsArtifactReference",
    "DooersThreadsMemory",
    "DooersThreadsThreadView",
    "AgentDirectory",
    "DooersThreadsGroupTurn",
    "decide_group_turn",
]


__all__ += [
    "DooersThreadsCallbackClient",
    "DooersThreadsCallbackError",
    "DooersThreadsHeartbeatGate",
    "DooersThreadsTextExecutor",
    "create_text_delivery_adapter",
    "DooersThreadsAgentMemory",
    "DooersThreadsExecutionPersistence",
    "DooersThreadsEventSink",
    "DooersThreadsOutputIntent",
    "DooersThreadsSend",
    "DooersThreadsTextStream",
    "create_durable_text_delivery_adapter",
    "run_dooers_threads_effect",
    "DooersThreadsEffectTool",
    "PostgresEffectJournal",
    "DooersThreadsCheckpoint",
    "DooersThreadsFormSuspension",
    "DooersThreadsCheckpointStore",
    "DooersThreadsApprovalRuntime",
    "DooersThreadsApprovalCommercialProof",
    "DooersThreadsApprovalOutcome",
    "run_dooers_threads_approved_effect",
]


def __getattr__(name: str):
    # AgentConfig importa dooers_threads.config durante startup legado. Handlers só são
    # carregados ao acessar o executor/sink, evitando ciclo e extras implícitos.
    from importlib import import_module

    modules = {
        "DooersThreadsTextExecutor": ".executor",
        "create_text_delivery_adapter": ".executor",
        "DooersThreadsAgentApi": ".agent_api",
        "DooersThreadsAgentApiError": ".agent_api",
        "DooersThreadsAgentMissing": ".agent_api",
        "DooersThreadsAgentNotFound": ".agent_api",
        "DooersThreadsAgentThreadDeleting": ".agent_api",
        "DooersThreadsAgentScopeError": ".agent_api",
        "DooersThreadsAgentPersistence": ".agent_persistence",
        "DooersThreadsAgentUnsupported": ".agent_persistence",
        "DooersThreadsAgentMemory": ".persistence",
        "DooersThreadsExecutionPersistence": ".persistence",
        "DooersThreadsEventSink": ".sink",
        "DooersThreadsOutputIntent": ".sink",
        "DooersThreadsSend": ".sink",
        "DooersThreadsTextStream": ".streaming",
        "create_durable_text_delivery_adapter": ".journal_runtime",
        "run_dooers_threads_effect": ".journal_runtime",
        "DooersThreadsEffectTool": ".journal_runtime",
        "PostgresEffectJournal": ".journal_store",
        "DooersThreadsCheckpoint": ".interactions",
        "DooersThreadsFormSuspension": ".interactions",
        "DooersThreadsCheckpointStore": ".interactions",
        "DooersThreadsApprovalRuntime": ".approvals",
        "DooersThreadsApprovalCommercialProof": ".approvals",
        "DooersThreadsApprovalOutcome": ".approvals",
        "run_dooers_threads_approved_effect": ".approvals",
    }
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(modules[name], __name__), name)


__all__ += ["DooersThreadsArtifactsClient", "DooersThreadsHydratedArtifact"]
__all__ += [
    "DooersThreadsAgentApi",
    "DooersThreadsAgentApiError",
    "DooersThreadsAgentMissing",
    "DooersThreadsAgentNotFound",
    "DooersThreadsAgentPersistence",
    "DooersThreadsAgentScopeError",
    "DooersThreadsAgentThreadDeleting",
    "DooersThreadsAgentUnsupported",
]
