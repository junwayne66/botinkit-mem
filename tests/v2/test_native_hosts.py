"""Gate-visible wrapper for the real Claude Code and Codex host check.

tests/v2/native_hosts.py is a script: it has no TestCase and was therefore
invisible to `unittest discover`, which is how the previous round recorded
"two-host receipt checks passed" while the suite never ran it in a gate. This
module keeps native_hosts.py as the implementation (single place to maintain
the host fixtures) and makes its result a hard, counted test case.

Opt-in guard mirrors tests/v2/test_integration.py:692-697 so run_gates.py can
predict the expected skip set: baseline profile strips the variable and treats
this case as an expected skip, the full-evidence profile must set
BMEM_V2_TEST_NATIVE_HOSTS=1 and then requires zero skips.

Scope note: today native_hosts.py asserts that loaded and used stay unknown,
because no product path reports them. That is the gap T052 closes; when the
assertion flips to real receipts, this wrapper does not change - the markers
below and the exit code are what the gate looks at.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPT = HERE / "native_hosts.py"
ARTIFACTS = HERE / "artifacts"

# Stable lines printed by each native_hosts.py stage, in the order they run.
MARKERS = (
    "MCP bound client found the corrected statement and could not expand evidence",
    "Both hooks deduplicated retry, rejected same-ID changes and refused mismatched host or source",
    "Both host adapters preserved structured receipts and scrubbed nested credential fields",
    "received the other host's source events after explicit task and source grants",
    "captured the current declaration revisions and the real shell receipt",
    "native capture jobs and",
)
MIN_MARKER_HITS = 9  # 6 stages, of which the declaration and cross-host ones print per host


class NativeHosts(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("BMEM_V2_TEST_NATIVE_HOSTS") == "1",
        "set BMEM_V2_TEST_NATIVE_HOSTS to opt in to the installed Claude Code and Codex hosts",
    )
    def test_real_host_adapters_complete_every_stage(self):
        binary = os.environ.get("BMEM_V2_TEST_BIN")
        self.assertTrue(
            binary and Path(binary).is_file(),
            "BMEM_V2_TEST_BIN must point at a built bmem binary for the host check",
        )
        timeout = int(os.environ.get("BMEM_V2_TEST_HOST_TIMEOUT", "1500"))
        proc = subprocess.run(
            [sys.executable, "-u", str(SCRIPT)],
            cwd=str(REPO),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / "native_hosts_output.txt").write_text(
            f"# binary={binary}\n# returncode={proc.returncode}\n$ stdout\n{proc.stdout}\n$ stderr\n{proc.stderr}\n"
        )

        self.assertEqual(
            proc.returncode,
            0,
            "native host check failed:\n" + (proc.stdout + proc.stderr)[-4000:],
        )
        for noise in ("Traceback", "AssertionError"):
            self.assertNotIn(noise, proc.stdout + proc.stderr)

        hits = sum(proc.stdout.count(marker) for marker in MARKERS)
        self.assertGreaterEqual(
            hits,
            MIN_MARKER_HITS,
            "expected every stage marker; got %d\n%s" % (hits, proc.stdout[-2500:]),
        )
        for marker in MARKERS:
            self.assertIn(marker, proc.stdout, "missing stage marker: " + marker)


if __name__ == "__main__":
    unittest.main()
