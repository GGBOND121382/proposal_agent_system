from __future__ import annotations

import re
from typing import Any

from . import agent_prompt_kernel as _kernel
from .agent_prompt_kernel import AgentPromptKernelValidator


# Python's ``\w`` includes CJK characters.  The base pattern therefore missed
# values in natural Chinese prose such as “完成2个原型” and “2027年”.  Restrict
# the left boundary to ASCII identifier characters so Chinese-adjacent numbers
# still require explicit value/unit/object/condition binding.
_kernel.NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?")


class TrackBAgentPromptValidator(AgentPromptKernelValidator):
    """Production Track-B validator with schema-preserving integration reports.

    The underlying validator recomputes redundancy statistics from MAIN_BODY
    sections only.  The public Prompt output schema already contains the
    ``redundancy_report`` object, so Track B replaces that object in place and
    removes internal bookkeeping fields rather than extending the frozen
    Prompt/Schema interface.
    """

    def _replace_document_statistics_with_main_body_only(
        self,
        payload: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        super()._replace_document_statistics_with_main_body_only(payload, output)
        result = output.get("result")
        if not isinstance(result, dict):
            return
        result.pop("main_body_redundancy_report", None)
        report = result.get("redundancy_report")
        if isinstance(report, dict):
            report.pop("main_body_section_count", None)
            report.pop("excluded_appendix_section_ids", None)
