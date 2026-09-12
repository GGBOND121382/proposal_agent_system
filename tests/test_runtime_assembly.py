from __future__ import annotations

import warnings


def test_importing_app_does_not_monkey_patch_base_classes():
    import app  # noqa: F401
    from app.context import ContextBuilder as BaseContextBuilder
    from app.runtime_context import LiveContextBuilder
    from app.runtime_api import ContextBuilder as RuntimeContextBuilder

    assert BaseContextBuilder is not LiveContextBuilder
    assert RuntimeContextBuilder is LiveContextBuilder


def test_legacy_bootstrap_is_a_non_mutating_compatibility_hook():
    from app.context import ContextBuilder as before
    from app.runtime_bootstrap import install_runtime_extensions

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        install_runtime_extensions()
    from app.context import ContextBuilder as after

    assert after is before
    assert any(item.category is DeprecationWarning for item in caught)
