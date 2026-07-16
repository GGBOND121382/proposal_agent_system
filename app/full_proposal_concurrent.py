from __future__ import annotations

from .full_proposal_contract import (
    FULL_PROPOSAL_GROUP_ORDER,
    FULL_PROPOSAL_GROUPS,
    FullProposalContractMixin,
)
from .full_integration_critic import FullIntegrationCriticMixin
from .g3_cross_chapter import G3CrossChapterReviewMixin
from .full_proposal_sections import FullProposalSectionsMixin
from .full_proposal_workers import FullProposalWorkersMixin


class FullProposalConcurrentMixin(
    G3CrossChapterReviewMixin,
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
