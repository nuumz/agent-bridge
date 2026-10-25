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
import threading
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
    bin_dir, stub, cwd, work, threads, tasks = sys.argv[1:7]
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
        with ThreadPoolExecutor(int(threads)) as ex:           # the pool's `with` outside guarded_rc, as bridge-panel's
            guarded_rc("t", bw.write_rc, lambda: max(ex.map(ask, range(int(tasks)))))
    """)


def alive(pid: int) -> bool:
    """A process that still runs — a zombie (dead, not yet reaped by a container's init) does not count; waiting
    for a reap here is what used to push the verify tests into another test's 10 s kill-timer window."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def wait_for(pred, seconds: float = 10) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class WorkSigterm(unittest.TestCase):
    def kill_run(self, threads: int, sig: int, tasks: int | None = None) -> tuple[int, str, list[int], float, int]:
        """Start the driver with `tasks` asks over `threads` workers, wait for the peers that can run at once, send
        `sig`; return (rc, rc file, those peer pids, seconds to exit, how many peers ever started)."""
        running = max(threads, 1)
        tasks = tasks or running
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "ask-agent"
            stub.write_text(STUB)
            stub.chmod(0o755)
            cwd, work = Path(d) / "repo", Path(d) / "work"
            dirs = [cwd / str(i) for i in range(tasks)]
            for sub_dir in dirs:
                sub_dir.mkdir(parents=True)
            env = {**os.environ, "HOME": d}
            drv = subprocess.Popen([sys.executable, "-c", DRIVER, str(BIN), str(stub), str(cwd), str(work),
                                    str(threads), str(tasks)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   text=True, env=env)
            peers: list[int] = []
            started = 0
            try:
                for sub_dir in dirs[:running]:
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
                late = [int((s / "peer.pid").read_text()) for s in dirs if (s / "peer.pid").is_file()]
                started = len(late)
                for p in {*peers, *late}:                         # never leave a `sleep 300` behind a failed assertion
                    try:
                        os.killpg(p, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            rc_file = (work / "t" / "rc").read_text() if (work / "t" / "rc").is_file() else ""
            return drv.returncode, rc_file, peers, took, started

    def test_sigterm_to_bridge_work_ends_the_peer_and_still_writes_143(self):
        rc, rc_file, peers, took, _ = self.kill_run(0, signal.SIGTERM)
        self.assertEqual((rc, rc_file), (143, "143\n"))
        self.assertTrue(wait_for(lambda: not alive(peers[0]), 10), "the peer outlived the killed bridge-work run")
        self.assertLess(took, 8, "rc must be written within the kill grace, not after the peer's -t")

    def test_sigterm_during_a_panels_parallel_phase_ends_every_peer(self):
        rc, rc_file, peers, took, _ = self.kill_run(2, signal.SIGTERM)
        self.assertEqual((rc, rc_file), (143, "143\n"))
        for p in peers:
            self.assertTrue(wait_for(lambda: not alive(p), 10), "a worker-thread peer outlived the killed panel")
        self.assertLess(took, 8)

    def test_sighup_is_handled_like_sigterm(self):
        rc, rc_file, peers, _, _ = self.kill_run(0, signal.SIGHUP)
        self.assertEqual((rc, rc_file), (129, "129\n"))
        self.assertTrue(wait_for(lambda: not alive(peers[0]), 10))

    def test_no_peer_starts_after_the_kill(self):
        """A panel with more roles than --parallel: the pool's queued asks must not spawn a peer after the kill. The
        pool's `with` is outside guarded_rc, so shutdown(wait=True) would run them; `ex.map` cancels its queue when
        the kill interrupts the result loop, and core._STOP keeps run_tool from spawning for any other pool shape."""
        rc, rc_file, peers, took, started = self.kill_run(1, signal.SIGTERM, tasks=2)
        self.assertEqual((rc, rc_file), (143, "143\n"))
        self.assertTrue(wait_for(lambda: not alive(peers[0]), 10))
        self.assertEqual(started, 1, "the second ask was queued behind the kill and must never start a peer")
        self.assertLess(took, 8)

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

    def test_verify_timeout_returns_when_a_detached_descendant_holds_the_pipes(self):
        """A build daemon that called setsid() is outside the group run_tool kills, and it keeps stdout/stderr open:
        the second communicate() must give up on the pipes instead of waiting for it forever."""
        loader = importlib.machinery.SourceFileLoader("bw_verify3", str(BIN / "bridge-work"))
        bw = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(bw)
        with tempfile.TemporaryDirectory() as d:
            daemon = f"{sys.executable} -c 'import os, time; os.setsid(); time.sleep(300)'"
            result: list = []
            t = threading.Thread(target=lambda: result.append(
                bw.verify(Path(d), f"{daemon} & echo $! > child.pid; wait", timeout=1)), daemon=True)
            t0 = time.time()
            t.start()
            t.join(timeout=40)
            self.assertTrue(wait_for(lambda: (Path(d) / "child.pid").is_file(), 5))
            child = int((Path(d) / "child.pid").read_text())
            self.addCleanup(lambda: os.kill(child, signal.SIGKILL) if alive(child) else None)
            self.assertFalse(t.is_alive(), "verify hung on pipes a detached descendant holds")
            self.assertEqual(result[0][0], 124)
            self.assertLess(time.time() - t0, 30)

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
