"""Compatibility facade re-exporting pure open-source runtime primitives from runtime_core."""
import hashlib
import json
import os
import shutil
import sys

from runtime_core import (  # noqa: F401
    AUDIO_EXT,
    EASINGS,
    IMAGE_EXT,
    KEYFRAMES,
    MEDIA_EXT,
    SKILL_ROOT,
    TICKS_PER_SEC,
    TRANSFORMS,
    VIDEO_EXT,
    check_text,
    composite_range,
    emit,
    end_us,
    fields,
    local_media_path,
    new_directory,
    number,
    probe,
    provenance,
    py,
    read_json,
    run,
    snap_range,
    source_delivery,
    source_limit_us,
    tool,
    tool_path,
    us,
    validate_parameter_keys,
    validate_plan,
    write_json,
)

try:
    import filmcraft_backend
except ImportError:
    filmcraft_backend = None


def doctor():
    import open_engine
    return open_engine.doctor()


def capabilities(*args, **kwargs):
    import open_engine
    return open_engine.capabilities(*args, **kwargs)


def configured_user_data():
    return None


def build_draft(args):
    """Build open-source FilmCraft/EffectCraft project from validated edit plan."""
    import open_engine
    plan = getattr(args, 'plan')
    out = getattr(args, 'out')
    return open_engine.build(plan, out)
