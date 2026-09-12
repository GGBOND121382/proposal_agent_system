from __future__ import annotations

import warnings


def install_runtime_extensions() -> None:
    """Deprecated compatibility hook.

    Runtime implementations are now assembled explicitly by
    :func:`app.runtime_factory.build_runtime_stack`. This function is retained
    for callers that still import it, but it intentionally performs no module
    mutation.
    """
    warnings.warn(
        "install_runtime_extensions() is deprecated; use build_runtime_stack()",
        DeprecationWarning,
        stacklevel=2,
    )
