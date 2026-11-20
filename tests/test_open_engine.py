"""Direct openedit_engine & jianying_engine compatibility wrapper tests covering open_engine_self_check."""
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'src/openedit_engine/runtime/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from open_engine_self_check import OpenEngineTests  # noqa: F401, E402


class CompatibilityAliasTests(unittest.TestCase):
    def test_jianying_engine_cli_and_package_aliases_match_openedit_engine(self):
        import openedit_engine
        import jianying_engine
        from openedit_engine import api as oe_api, cli as oe_cli, mcp_server as oe_mcp
        from jianying_engine import api as jy_api, cli as jy_cli, mcp_server as jy_mcp

        self.assertEqual(jianying_engine.__version__, openedit_engine.__version__)
        self.assertIs(jy_api, oe_api)
        self.assertIs(jy_cli, oe_cli)
        self.assertIs(jy_mcp, oe_mcp)

        oe_ver = subprocess.run([sys.executable, '-m', 'openedit_engine', '--version'],
                                capture_output=True, text=True, check=True)
        jy_ver = subprocess.run([sys.executable, '-m', 'jianying_engine', '--version'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(oe_ver.stdout.strip(), openedit_engine.__version__)
        self.assertEqual(jy_ver.stdout.strip(), openedit_engine.__version__)


if __name__ == '__main__':
    unittest.main()
