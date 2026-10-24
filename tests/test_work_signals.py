"""A kill of bridge-work / bridge-panel takes the running peer down with it. guarded_rc turns SIGTERM into exit 143
and writes the rc file; the peer call underneath must not be a plain subprocess.run, which SIGKILLs only ask-agent —
killed that way it never forwards the signal, and the peer (in write mode: still editing) outlives the run."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

# stands in for ask-agent: starts a "peer" in its own process group (as runner.run does), forwards SIGTERM to it and
# exits 143 — exactly what the real one does; a SIGKILL gives it no chance to
STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, signal, subprocess, sys
    cwd = sys.argv[sys.argv.index("-d") + 1]
    peer = subprocess.Popen(["sleep", "300"], process_group=0)
    open(os.path.join(cwd, "peer.pid.tmp"), "w").write(str(peer.pid))
    os.replace(os.path.join(cwd, "peer.pid.tmp"), os.path.join(cwd, "peer.pid"))
    def fwd(sig, _):
        os.killpg(peer.pid, signal.SIGTERM); sys.exit(143)   # no wait: the outer peer.wait() holds Popen's lock
    signal.signal(signal.SIGTERM, fwd)
    peer.wait()
    print("answer")
    """)

# the orchestrator side, run as its own process so the test can signal it: bridge-work's real ask() under guarded_rc,
# in the main thread (bridge-work) or in a thread pool (bridge-panel's worker and cross-examination phases)
DRIVER = textwrap.dedent("""\
    import importlib.machinery, importlib.util, sys
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    bin_dir, stub, cwd, work, threads = sys.argv[1:6]
    sys.path.insert(0, bin_dir)
    loader = importlib.machinery.SourceFileLoader("bw", str(Path(bin_dir) / "bridge-work"))
    bw = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(bw)
    bw.ASK, bw.WORK = Path(stub), Path(work)
    from bridgelib.core import guarded_rc
    ask = lambda i: bw.ask("claude", "write", cwd + "/" + str(i), "p", 60, "c", "k")[0]
    if threads == "0":
        guarded_rc("t", bw.write_rc, lambda: ask(0))
    else:
        with ThreadPoolExecutor(int(threads)) as ex:
            guarded_rc("t", bw.write_rc, lambda: max(ex.map(ask, range(int(threads)))))
    """)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for(pred, seconds: float = 10) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class WorkSigterm(unittest.TestCase):
    def kill_run(self, threads: int, sig: int) -> tuple[int, str, list[int], float]:
        """Start the driver, wait for every stub peer, send `sig`; return (rc, rc file, peer pids, seconds to exit)."""
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "ask-agent"
            stub.write_text(STUB)
            stub.chmod(0o755)
            cwd, work = Path(d) / "repo", Path(d) / "work"
            dirs = [cwd / str(i) for i in range(max(threads, 1))]
            for sub_dir in dirs:
                sub_dir.mkdir(parents=True)
            env = {**os.environ, "HOME": d}
            drv = subprocess.Popen([sys.executable, "-c", DRIVER, str(BIN), str(stub), str(cwd), str(work),
                                    str(threads)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                   env=env)
            peers: list[int] = []
            try:
                for sub_dir in dirs:
                    pid_file = sub_dir / "peer.pid"
                    self.assertTrue(wait_for(lambda: pid_file.is_file() and pid_file.read_text().strip() != ""),
                                    "a stub peer never started")
                    peers.append(int(pid_file.read_text()))
                self.assertTrue(all(alive(p) for p in peers))
                t0 = time.time()
                drv.send_signal(sig)
                _, err = drv.communicate(timeout=30)
                took = time.time() - t0
            finally:
                if drv.poll() is None:
                    drv.terminate()
                    try:
                        drv.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        drv.kill()
                        drv.wait()
                for p in peers:                                   # never leave a `sleep 300` behind a failed assertion
                    try:
                        os.killpg(p, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            rc_file = (work / "t" / "rc").read_text() if (work / "t" / "rc").is_file() else ""
            return drv.returncode, rc_file, peers, took

    def test_sigterm_to_bridge_work_ends_the_peer_and_still_writes_143(self):
        rc, rc_file, peers, took = self.kill_run(0, signal.SIGTERM)
        self.assertEqual((rc, rc_file), (143, "143\n"))
        self.assertTrue(wait_for(lambda: not alive(peers[0]), 10), "the peer outlived the killed bridge-work run")
        self.assertLess(took, 8, "rc must be written within the kill grace, not after the peer's -t")

    def test_sigterm_during_a_panels_parallel_phase_ends_every_peer(self):
        rc, rc_file, peers, took = self.kill_run(2, signal.SIGTERM)
        self.assertEqual((rc, rc_file), (143, "143\n"))
        for p in peers:
            self.assertTrue(wait_for(lambda: not alive(p), 10), "a worker-thread peer outlived the killed panel")
        self.assertLess(took, 8)

    def test_sighup_is_handled_like_sigterm(self):
        rc, rc_file, peers, _ = self.kill_run(0, signal.SIGHUP)
        self.assertEqual((rc, rc_file), (129, "129\n"))
        self.assertTrue(wait_for(lambda: not alive(peers[0]), 10))

    def test_verify_timeout_returns_even_when_a_child_ignores_sigterm(self):
        loader = importlib.machinery.SourceFileLoader("bw_verify2", str(BIN / "bridge-work"))
        bw = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(bw)
        with tempfile.TemporaryDirectory() as d:
            t0 = time.time()
            rc, _ = bw.verify(Path(d), "sh -c 'trap \"\" TERM; echo $$ > child.pid; sleep 300' & wait", timeout=1)
            self.assertEqual(rc, 124)
            self.assertLess(time.time() - t0, 25, "verify hung on a pipe held by a SIGTERM-ignoring child")
            child = int((Path(d) / "child.pid").read_text())
            self.assertTrue(wait_for(lambda: not alive(child), 10))

    def test_verify_timeout_ends_the_commands_children(self):
        loader = importlib.machinery.SourceFileLoader("bw_verify", str(BIN / "bridge-work"))
        bw = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(bw)
        with tempfile.TemporaryDirectory() as d:
            rc, tail = bw.verify(Path(d), "sleep 300 & echo $! > child.pid; wait", timeout=1)
            self.assertEqual(rc, 124)
            self.assertIn("timed out", tail)
            child = int((Path(d) / "child.pid").read_text())
            self.assertTrue(wait_for(lambda: not alive(child), 10), "the verify command's child outlived the timeout")


if __name__ == "__main__":
    unittest.main()
