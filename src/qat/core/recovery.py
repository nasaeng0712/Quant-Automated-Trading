"""Authorization object for the explicit emergency-flatten recovery path.

Only ``issue_recovery_authorization`` (called by ``qat.ops.recovery.RecoveryController``) can mint a valid authorization. Validity is decided by a
module-private registry of 128-bit random tokens, never by anything the object itself claims: a client-supplied dict, string, hand-built instance
or copy with a guessed token is invalid. The Kill Switch stays a hard stop for the normal order pipeline: a flatten does not release it, and a
flatten is never started automatically.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

_ISSUED_TOKENS: set[str] = set()


@dataclass(frozen=True)
class RecoveryAuthorization:
    operator: str
    reason: str
    recovery_id: str
    token: str = field(repr=False, compare=False, default="")

    def is_valid(self) -> bool:
        return bool(self.token) and self.token in _ISSUED_TOKENS and bool(str(self.operator).strip()) and bool(str(self.reason).strip())


def issue_recovery_authorization(operator: str, reason: str, recovery_id: str) -> RecoveryAuthorization:
    token = secrets.token_hex(16)
    _ISSUED_TOKENS.add(token)
    return RecoveryAuthorization(operator=operator, reason=reason, recovery_id=recovery_id, token=token)
