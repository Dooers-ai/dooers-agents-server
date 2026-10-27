"""Admissão Maestro opt-in; nenhum handler, broker ou cliente é ativado por import."""

from .adapter import (
    MaestroAdmission,
    MaestroAdmissionError,
    MaestroConfirmedResult,
    MaestroCurrentLease,
    MaestroDeliveryAdapter,
    MaestroExecutor,
    MaestroLeaseGate,
)
from .artifacts import MaestroArtifactsClient, MaestroHydratedArtifact
from .callbacks import MaestroCallbackClient, MaestroCallbackError, MaestroHeartbeatGate
from .config import MaestroConfig, MaestroConfigurationError, MaestroCredentialRequest, MaestroInstanceCredential

__all__ = [
    "MaestroAdmission",
    "MaestroAdmissionError",
    "MaestroConfig",
    "MaestroConfigurationError",
    "MaestroConfirmedResult",
    "MaestroCredentialRequest",
    "MaestroCurrentLease",
    "MaestroDeliveryAdapter",
    "MaestroExecutor",
    "MaestroInstanceCredential",
    "MaestroLeaseGate",
]

from .client import MaestroContextClient, MaestroContextError
from .context import (
    MaestroCancellation,
    MaestroExecutionContext,
    MaestroHandlerInput,
    MaestroPrivateResolver,
    MaestroPrivateResources,
    MaestroPrivateScope,
    current_maestro_context,
    maestro_handler_context,
)
from .group import AgentDirectory, MaestroGroupTurn, decide_group_turn
from .memory import MaestroArtifactReference, MaestroMemory, MaestroThreadView

__all__ += [
    "MaestroCancellation",
    "MaestroContextClient",
    "MaestroContextError",
    "MaestroExecutionContext",
    "MaestroHandlerInput",
    "MaestroPrivateResolver",
    "MaestroPrivateResources",
    "MaestroPrivateScope",
    "current_maestro_context",
    "maestro_handler_context",
    "MaestroArtifactReference",
    "MaestroMemory",
    "MaestroThreadView",
    "AgentDirectory",
    "MaestroGroupTurn",
    "decide_group_turn",
]


__all__ += [
    "MaestroCallbackClient",
    "MaestroCallbackError",
    "MaestroHeartbeatGate",
    "MaestroTextExecutor",
    "create_text_delivery_adapter",
    "MaestroEventSink",
    "MaestroOutputIntent",
    "MaestroSend",
    "MaestroTextStream",
    "create_durable_text_delivery_adapter",
    "run_maestro_effect",
    "MaestroEffectTool",
    "PostgresEffectJournal",
    "MaestroCheckpoint",
    "MaestroFormSuspension",
    "MaestroCheckpointStore",
    "MaestroApprovalRuntime",
    "MaestroApprovalCommercialProof",
    "MaestroApprovalOutcome",
    "run_maestro_approved_effect",
]


def __getattr__(name: str):
    # AgentConfig importa maestro.config durante startup legado. Handlers só são
    # carregados ao acessar o executor/sink, evitando ciclo e extras implícitos.
    from importlib import import_module

    modules = {
        "MaestroTextExecutor": ".executor",
        "create_text_delivery_adapter": ".executor",
        "MaestroEventSink": ".sink",
        "MaestroOutputIntent": ".sink",
        "MaestroSend": ".sink",
        "MaestroTextStream": ".streaming",
        "create_durable_text_delivery_adapter": ".journal_runtime",
        "run_maestro_effect": ".journal_runtime",
        "MaestroEffectTool": ".journal_runtime",
        "PostgresEffectJournal": ".journal_store",
        "MaestroCheckpoint": ".interactions",
        "MaestroFormSuspension": ".interactions",
        "MaestroCheckpointStore": ".interactions",
        "MaestroApprovalRuntime": ".approvals",
        "MaestroApprovalCommercialProof": ".approvals",
        "MaestroApprovalOutcome": ".approvals",
        "run_maestro_approved_effect": ".approvals",
    }
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(modules[name], __name__), name)


__all__ += ["MaestroArtifactsClient", "MaestroHydratedArtifact"]
