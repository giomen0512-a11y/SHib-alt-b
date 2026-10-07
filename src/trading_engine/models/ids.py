"""IDs suministrados por el llamador; NewType distingue entidades en mypy."""

from typing import NewType
from uuid import UUID

RoundId = NewType("RoundId", UUID)
SignalId = NewType("SignalId", UUID)
IntentId = NewType("IntentId", UUID)
OrderId = NewType("OrderId", UUID)
FillId = NewType("FillId", UUID)
