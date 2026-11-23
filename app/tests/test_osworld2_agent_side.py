from __future__ import annotations

import base64
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(APP, "nullius", "catalogue", "benchmarks", "osworld2")
FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "osworld2_fake_desktop")
OSWORLD2 = os.environ.get("OSWORLD2_DIR", "")
PYTHON = os.environ.get("OSWORLD2_PYTHON", "")
LOOP = ("lib_run_single.py", "lib_results_logger.py", "task_loader.py", "mm_agents",
        os.path.join("desktop_env", "task_base.py"))
WHY = ("OSWORLD2_DIR does not name a checkout of the upstream" if not all(os.path.exists(os.path.join(OSWORLD2, n)) for n in LOOP)
       else "OSWORLD2_PYTHON does not name the upstream's Python" if not os.path.isfile(PYTHON)
       else "")
INSTRUCTION = "Write the word note in the open editor."
ONE = '''from desktop_env.task_base import BaseTask


class Task(BaseTask):
    id = "901"
    instruction = %r
    task_current_date = "2031-02-03"

    def evaluate(self, env):
        return 1.0 if "pyautogui.press('enter')" in env.actions and env.actions[-1] == "DONE" else 0.0


def get_task():
    return Task()
''' % INSTRUCTION
PHASES = '''from desktop_env.task_base import MultiPhaseTask


class Task(MultiPhaseTask):
    id = "902"
    instruction = "First part."

    def get_phases(self):
        return [{"instruction": "First part.", "evaluate": lambda env: 0.0},
                {"instruction": "Second part.", "setup": lambda c, use_proxy=False: None, "evaluate": lambda env: 0.0}]


def get_task():
    return Task()
'''
INFRA = '''from desktop_env.evaluators.errors import EvaluationInfrastructureError
from desktop_env.task_base import BaseTask


class Task(BaseTask):
    id = "903"
    instruction = "Draw a part."

    def evaluate(self, env):
        EvaluationInfrastructureError("a program the evaluator needs is missing", subtype="missing")
        return 0.0


def get_task():
    return Task()
'''
UNIT = "tasks__901"
CLICK = "pyautogui.click(10, 20, clicks=1, button='left')"
ENTER = "pyautogui.press('enter')"


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def state(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def gone(pid: int, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


class Session:
    def __init__(self, setup: dict, cwd: str):
        self.pipe = subprocess.Popen([sys.executable, *setup["mcp"][0]["args"]], cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.got, self.last = b"", 0

    def ask(self, method: str, params: dict | None = None, seconds: float = 60.0) -> dict:
        self.last += 1
        message = {"jsonrpc": "2.0", "id": self.last, "method": method, **({"params": params} if params else {})}
        self.pipe.stdin.write((json.dumps(message) + "\n").encode())
        self.pipe.stdin.flush()
        deadline = time.monotonic() + seconds
        while True:
            while b"\n" in self.got:
                line, _, self.got = self.got.partition(b"\n")
                reply = json.loads(line)
                if reply.get("id") == self.last:
                    return reply
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.pipe.stdout], [], [], left)[0]:
                raise AssertionError(f"no answer to {method} within {seconds} s")
            chunk = os.read(self.pipe.stdout.fileno(), 1 << 20)
            if not chunk:
                raise AssertionError("the session's pipe ended")
            self.got += chunk

    def call(self, tool: str, **arguments) -> dict:
        return self.ask("tools/call", {"name": tool, "arguments": arguments})["result"]

    def close(self) -> None:
        self.pipe.stdin.close()
        try:
            self.pipe.wait(timeout=30)
        finally:
            if self.pipe.poll() is None:
                self.pipe.kill()
                self.pipe.wait()
            self.pipe.stdout.close()


def value(result: dict) -> dict:
    return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))


@unittest.skipIf(WHY, WHY)
class AgentSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osworld2-agent-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        up = os.path.join(self.tmp, "upstream")
        classes = os.path.join(up, "evaluation_examples", "task_class")
        os.makedirs(classes)
        for name in LOOP[:4]:
            os.symlink(os.path.join(OSWORLD2, name), os.path.join(up, name))
        shutil.copytree(os.path.join(FAKE, "desktop_env"), os.path.join(up, "desktop_env"))
        shutil.copyfile(os.path.join(OSWORLD2, "desktop_env", "task_base.py"), os.path.join(up, "desktop_env", "task_base.py"))
        os.makedirs(os.path.join(up, "desktop_env", "evaluators"))
        open(os.path.join(up, "desktop_env", "evaluators", "__init__.py"), "w").close()
        shutil.copyfile(os.path.join(OSWORLD2, "desktop_env", "evaluators", "errors.py"),
                        os.path.join(up, "desktop_env", "evaluators", "errors.py"))
        for eid, text in (("901", ONE), ("902", PHASES), ("903", INFRA)):
            with open(os.path.join(classes, f"task_{eid}.py"), "w") as f:
                f.write(text)
        self.image = os.path.join(self.tmp, "image.qcow2")
        open(self.image, "w").close()
        self.log = os.path.join(self.tmp, "desktop.jsonl")
        self.trial = os.path.join(self.tmp, "job", "tasks__901__u0000")
        self.ws = os.path.join(self.trial, "workspace")
        os.makedirs(self.ws)
        no_programs = os.path.join(self.tmp, "no-programs")
        os.mkdir(no_programs)
        for patch in (mock.patch.dict(os.environ, {"NULLIUS_UPSTREAM": up, "FAKE_DESKTOP_LOG": self.log,
                                                   "PATH": no_programs, "PYTHONDONTWRITEBYTECODE": "1"}),
                      mock.patch.object(sys, "executable", PYTHON)):
            patch.start()
            self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location("osworld2_driver_agent", os.path.join(HERE, "driver.py"))
        self.d = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "argv", ["driver.py"]), mock.patch.object(sys, "path", [HERE, *sys.path]):
            spec.loader.exec_module(self.d)
        self.d.START_LOCK = os.path.join(self.tmp, "start.lck")
        self.d._ready = lambda domain, eid: self.image
        self.sessions = []
        self.addCleanup(self.end)

    def end(self):
        for s in self.sessions:
            if s.pipe.poll() is None:
                s.pipe.kill()
                s.pipe.wait()
        for where in list(self.d.RUNNERS):
            self.d._release(where)

    def options(self, **options):
        os.environ.update({"NULLIUS_OPTION_" + k.upper(): v for k, v in options.items()})
        self.addCleanup(lambda: [os.environ.pop("NULLIUS_OPTION_" + k.upper(), None) for k in options])

    def docker(self) -> str:
        calls, docker = os.path.join(self.tmp, "docker.calls"), os.path.join(os.environ["PATH"], "docker")
        with open(docker, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {calls}\n')
        os.chmod(docker, 0o755)
        return calls

    def docker_ps(self, calls, seconds: float = 10.0) -> bool:
        deadline = time.time() + seconds
        while True:
            try:
                with open(calls) as f:
                    if "ps -a -q --no-trunc --filter ancestor=" + self.d.PROVIDER_IMAGE in f.read().splitlines():
                        return True
            except FileNotFoundError:
                pass
            if time.time() > deadline:
                return False
            time.sleep(0.1)

    def setup(self, unit=UNIT) -> dict:
        return self.d.setup(unit=unit, dir=self.ws, participant="agent")

    def session(self, setup: dict) -> Session:
        s = Session(setup, self.ws)
        self.sessions.append(s)
        s.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        return s

    def score(self) -> dict:
        return self.d.score(unit=UNIT, dir=self.ws, transcript=[], answer="")

    def events(self, kind: str) -> list:
        with open(self.log) as f:
            return [e for e in map(json.loads, f) if e["event"] == kind]

    def traj(self) -> list:
        with open(os.path.join(self.trial, ".work", "osworld2", "results", "tasks", "901", "traj.jsonl")) as f:
            return [json.loads(line) for line in f]

    def runner(self) -> int:
        with open(os.path.join(self.trial, ".work", "osworld2", "runner.json")) as f:
            return json.load(f)["pid"]

    def test_the_v2_loop_takes_empty_several_and_ending_steps(self):
        self.options(max_steps="6")
        setup = self.setup()
        self.assertEqual(setup["prompt"], INSTRUCTION + "\n\nThe current date is 2031-02-03.\n\nIf you need a password "
                         f"for sudo, the password of the computer is '{self.d.PASSWORD}'.")
        self.assertEqual(setup["prompt"].split("\n\n")[-1], self.d.PASSWORD_LINE.format(password=self.d.PASSWORD))
        self.assertEqual([(m["name"], m["server"]) for m in setup["mcp"]], [("computer", "computer")])
        s = self.session(setup)
        self.assertEqual(value(s.call("upload", path="/tmp/agent/in.txt",
                                      data=base64.b64encode(b"hello").decode()))["size"], 5)
        self.assertEqual(self.d.task_given(unit=UNIT, dir=self.ws), {})
        self.assertEqual(value(s.call("step", actions=[]))["steps_left"], 5)
        three = value(s.call("step", actions=["pyautogui.press('a')", "pyautogui.press('b')", "pyautogui.press('c')"],
                             files=["in.txt"]))
        self.assertEqual((three["steps_left"], three["files"]), (4, {"in.txt": base64.b64encode(b"hello").decode()}))
        self.assertEqual(value(s.call("click", x=10, y=20))["steps_left"], 3)
        self.assertEqual(value(s.call("step", actions=[ENTER], end="done")), {"steps_left": 2, "done": True})
        s.close()
        score = self.score()
        self.assertEqual((score["reward"], score["detail"]["tool_calls"], score["detail"]["last_action"]), (1.0, 5, "DONE"))
        self.assertEqual([(t["step_num"], t["action"], t["screenshot_file"] is not None) for t in self.traj()],
                         [(2, "pyautogui.press('a')", False), (2, "pyautogui.press('b')", False),
                          (2, "pyautogui.press('c')", True), (3, CLICK, True), (4, ENTER, False), (4, "DONE", True)])
        self.assertEqual([e["action"] for e in self.events("step")],
                         ["pyautogui.press('a')", "pyautogui.press('b')", "pyautogui.press('c')", CLICK, ENTER, "DONE"])
        self.assertEqual([e["path"] for e in self.events("get_file")], ["/tmp/agent/in.txt"])
        self.assertEqual([e["seconds"] for e in self.events("wait")], [60])
        self.assertEqual([(e["client_password"], e["enable_proxy"]) for e in self.events("started")], [(self.d.PASSWORD, True)])
        with open(os.path.join(self.trial, "verifier", "osworld2", "result.txt")) as f:
            self.assertEqual(float(f.read()), 1.0)
        self.assertTrue(gone(self.runner()))
        self.assertEqual(self.d.RUNNERS, {})
        self.assertFalse(os.path.exists(os.path.dirname(setup["mcp"][0]["args"][1])))

    def test_the_turns_end_ends_the_loop_with_done(self):
        self.options(max_steps="50")
        calls = self.docker()
        s = self.session(self.setup())
        self.assertTrue(self.docker_ps(calls))
        self.d.task_given(unit=UNIT, dir=self.ws)
        s.call("click", x=10, y=20)
        s.close()
        runner, t0 = self.runner(), time.time()
        score = self.score()
        self.assertEqual(state(runner), "Z")
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(score["reward"], 0.0)
        self.assertEqual([(t["step_num"], t["action"]) for t in self.traj()], [(1, CLICK), (2, "DONE")])
        self.assertEqual([e["last"] for e in self.events("evaluate")], ["DONE"])

    def test_an_evaluation_whose_dependency_failed_is_the_environments_error_not_a_0(self):
        self.docker()
        s = self.session(self.setup("tasks__903"))
        self.d.task_given(unit="tasks__903", dir=self.ws)
        s.close()
        with self.assertRaises(self.d.EnvironmentSetupError) as raised:
            self.d.score(unit="tasks__903", dir=self.ws, transcript=[], answer="")
        self.assertIn("EvaluationInfrastructureError", str(raised.exception))
        self.assertNotIn("missing", str(raised.exception))
        self.assertFalse(os.path.exists(os.path.join(self.trial, "verifier", "osworld2", "result.txt")))

    def test_a_step_with_no_action_and_an_end_ends_the_task(self):
        s = self.session(self.setup())
        self.d.task_given(unit=UNIT, dir=self.ws)
        self.assertEqual(value(s.call("step", actions=[], end="fail", response="no such file")),
                         {"steps_left": 99, "done": True})
        s.close()
        self.assertEqual(self.score()["reward"], 0.0)
        self.assertEqual([(t["step_num"], t["action"], t["response"]) for t in self.traj()], [(1, "FAIL", "no such file")])

    def test_a_multi_phase_task_is_refused_before_the_agent(self):
        with self.assertRaisesRegex(self.d.EnvironmentSetupError, "a multi-phase task: agent.computer/v1 gives the "
                                                                  "agent one instruction"):
            self.setup("tasks__902")
        self.assertEqual(state(self.runner()), "Z")
        self.assertFalse(os.path.exists(self.log) and self.events("started"))
        self.assertEqual(self.d.RUNNERS, {})

    def test_the_reaper_signals_only_the_process_it_was_given(self):
        p = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        start = self.d._start_time(p.pid)
        self.d.reap(p.pid, self.tmp, False, start=start + 1)
        self.assertTrue(alive(p.pid))
        self.d.reap(p.pid, self.tmp, False, start=start)
        self.assertTrue(gone(p.pid))

    def test_the_reaper_ends_a_group_whose_leader_is_gone(self):
        leader = subprocess.Popen([PYTHON, "-c", "import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', "
                                   "'import time; time.sleep(60)']); print(c.pid, flush=True)"],
                                  start_new_session=True, stdout=subprocess.PIPE, text=True)
        child = int(leader.stdout.readline())
        start = self.d._start_time(leader.pid)
        leader.wait()
        leader.stdout.close()
        self.assertTrue(alive(child))
        self.d.reap(leader.pid, self.tmp, False, start=start)
        self.assertTrue(gone(child))

    def test_the_units_end_ends_a_runner_that_never_reached_score(self):
        setup = self.setup()
        runner = self.runner()
        self.assertEqual(self.d.end(unit=UNIT, dir=self.ws), {})
        self.assertTrue(gone(runner))
        self.assertFalse(os.path.exists(os.path.dirname(setup["mcp"][0]["args"][1])))
        self.assertEqual(self.d.end(unit=UNIT, dir=self.ws), {})
        with self.assertRaisesRegex(RuntimeError, "no runner of this driver's for this unit"):
            self.score()

    def test_a_runner_whose_reaper_cannot_start_is_ended_and_its_vms_removed_at_once(self):
        arm, started, calls = self.d._arm, [], self.docker()

        def failing(pgid, dir, vms, start=None):
            if not vms:
                return arm(pgid, dir, vms, start)
            started.append(pgid)
            raise OSError("no reaper")

        self.d._arm, self.d.SWEEPS = failing, (0,)
        with self.assertRaisesRegex(OSError, "no reaper"):
            self.setup()
        self.assertIsNone(state(started[0]))
        self.assertEqual(self.d.RUNNERS, {})
        self.assertTrue(self.docker_ps(calls))

    def test_tasks_the_port_cannot_serve_are_refused_before_a_vm(self):
        for unit, said in (("tasks__003", "LLM grader"), ("tasks__024", "simulated user")):
            with self.assertRaisesRegex(self.d.EnvironmentSetupError, said):
                self.setup(unit)
        self.assertFalse(os.path.exists(os.path.join(self.trial, ".work")))


if __name__ == "__main__":
    unittest.main()
