from __future__ import annotations

from .full_proposal_contract import (
    FULL_PROPOSAL_GROUP_ORDER,
    FULL_PROPOSAL_GROUPS,
    FullProposalContractMixin,
)
from .full_integration_critic import FullIntegrationCriticMixin
from .full_proposal_sections import FullProposalSectionsMixin
from .full_proposal_workers import FullProposalWorkersMixin


class FullProposalConcurrentMixin(
    FullIntegrationCriticMixin,
    FullProposalSectionsMixin,
    FullProposalWorkersMixin,
    FullProposalContractMixin,
):
    pass


__all__ = [
    "FULL_PROPOSAL_GROUP_ORDER",
    "FULL_PROPOSAL_GROUPS",
    "FullProposalConcurrentMixin",
]
