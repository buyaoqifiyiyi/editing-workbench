"""Run the music-edit payload, action-anchor, and open backend compile regression through unittest."""
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from music_edit_self_check import MusicEditTests  # noqa: F401, E402
