from __future__ import annotations

from .full_proposal_concurrent import (
    FULL_PROPOSAL_GROUP_ORDER,
    FULL_PROPOSAL_GROUPS,
    FullProposalConcurrentMixin,
)
from .full_proposal_repair import FullProposalRepairMixin
from .workflow_authoring_base import (
    THREE_SECTION_PROFILE_ORDER,
    WorkflowAuthoringMixin as BaseWorkflowAuthoringMixin,
)


class WorkflowAuthoringMixin(
    FullProposalRepairMixin,
    FullProposalConcurrentMixin,
    BaseWorkflowAuthoringMixin,
):
    pass


__all__ = [
    "FULL_PROPOSAL_GROUP_ORDER",
    "FULL_PROPOSAL_GROUPS",
    "THREE_SECTION_PROFILE_ORDER",
    "WorkflowAuthoringMixin",
]
