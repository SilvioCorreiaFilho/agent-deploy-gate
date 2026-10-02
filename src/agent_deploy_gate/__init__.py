"""agent-deploy-gate — um gate de deploy determinístico para cargas de agente.

O problema que resolve: agente de IA aplica mudança em produção e, quando algo
falha, ninguém sabe qual era o estado anterior. Este pacote transforma o deploy
em algo com as mesmas garantias que a gente exige de código de pagamento:
snapshot antes, invariantes verificados, verificação em degraus depois e
rollback automático quando a verificação não passa.

Sem dependências externas — só stdlib.
"""
from .gate import (
    DeployGate,
    DeployResult,
    Invariant,
    InvariantViolation,
    LocalDirTransport,
    CommandTransport,
    VerifyResult,
    contains,
    min_size,
    not_contains,
)
from .audit import AuditLog

__version__ = '1.0.0'
__all__ = [
    'DeployGate', 'DeployResult', 'Invariant', 'InvariantViolation',
    'LocalDirTransport', 'CommandTransport', 'VerifyResult', 'AuditLog',
    'min_size', 'contains', 'not_contains',
]