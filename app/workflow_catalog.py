from __future__ import annotations

from .staged_workflows import STAGED_STEPS, STAGED_WORKFLOW_TYPE
from .workflow_defs import WORKFLOWS


ALL_WORKFLOWS = {
    **WORKFLOWS,
    STAGED_WORKFLOW_TYPE: [
        {"type": "STAGED", "stage": stage, "execution_style": "FILE_BRIDGED"}
        for stage in STAGED_STEPS
    ],
}
