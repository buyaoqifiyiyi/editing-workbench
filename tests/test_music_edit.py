"""Run the same native-media/action-anchor regression through the package test runner."""
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/jianying_engine/runtime/scripts'
sys.path.insert(0, str(SCRIPTS))
try:
    from music_edit_self_check import MusicEditTests
finally:
    sys.path.pop(0)
