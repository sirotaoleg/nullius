"""A unit that needs a port the agent has not proven is not run: it scores 0 as its own outcome."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from nullius.runs import attempts, classify, parse
from nullius.standard import definitions, install, job as jobs, merge, run

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_osworld2_judge import load_driver  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "standard", "catalogue")
CONVERSATION = "agent.conversation/v1"
BENCH = {
    "schema": "nullius.benchmark/v1", "key": "fixture", "label": "Fixture", "summary": "A task.",
    "family": "agent", "needs": ["agent.task/v1"],
    "driver": {"command": ["python3", "{here}/driver.py"]},
    "datasets": [{"id": "all", "units": 2}],
    "units": {"noun": "task", "nouns": "tasks"},
    "controls": {"nop": {"source": "nullius", "from": "nothing"}},
}
DRIVER = '''import os
from nullius.standard.jsonl import serve
def setup(unit, dir, **_):
    with open(os.path.join(os.path.dirname(os.path.dirname(dir)), "setup-" + unit), "w"):
        pass
    return {"prompt": "hi"}
serve({"units": lambda dataset: {"units": ["a", "b"], "needs": {"b": ["agent.conversation/v1"]}}, "setup": setup,
       "score": lambda unit, dir, transcript, answer: {"reward": REWARD, "passed": REWARD == 1.0, "detail": {}}})
'''


class NoPort(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home, install.HOME = install.HOME, os.path.join(self.tmp, "home")
        self.addCleanup(setattr, install, "HOME", home)
        for name, value in (("unit_relay", None), ("drive_agent", ("answer", None, {}))):
            patch = mock.patch.object(run.Run, name, return_value=value)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def go(self, other, kind="score", reward=0.5, **kw):
        here = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(here, "driver.py"), "w") as f:
            f.write(DRIVER.replace("REWARD", repr(reward)))
        b = dict(BENCH, _here=here, _kind="benchmark", score={"kind": kind, "says": "the answer"})
        job = run.run(b, other, model="m-1", provider="custom", jobs_root=tempfile.mkdtemp(dir=self.tmp), concurrency=1,
                      env={"MODEL_API_URL": "http://127.0.0.1:9/v1"}, say=lambda s: None, unsandboxed=True, **kw)
        trials = {}
        for d in sorted(os.listdir(job)):
            if "__u" in d:
                with open(os.path.join(job, d, "result.json")) as f:
                    trials[d] = json.load(f)
        return job, trials

    def agent_def(self, proven=False):
        a = copy.deepcopy(definitions.get("agent", "fake", ROOT))
        if proven:
            a["offers"] = a["offers"] + [CONVERSATION]
            a["proven"] = dict(a["proven"], **{CONVERSATION: dict(a["proven"]["agent.task/v1"])})
        return a

    def test_an_agent_without_the_port_scores_0_on_that_unit_and_it_is_never_set_up(self):
        job, trials = self.go(self.agent_def())
        b = trials["b__u0001"]
        self.assertEqual(b["exception_info"]["exception_type"], jobs.NO_PORT)
        self.assertEqual(b["exception_info"]["exception_message"], f"not run: needs {CONVERSATION}, which Fake agent has not proven")
        self.assertFalse(os.path.exists(os.path.join(job, "b__u0001", "agent", "trajectory.json")))
        with open(os.path.join(job, "progress.jsonl")) as f:
            ended = {r["unit"]: r["outcome"] for r in map(json.loads, f) if r.get("stage") == "unit" and r.get("state") == "done"}
        self.assertEqual(ended, {"a": "scored", "b": "scored"})
        self.assertIs(b["exception_info"]["retryable"], False)
        self.assertEqual(b["verifier_result"]["rewards"], {"score": 0.0})
        self.assertIsNone(trials["a__u0000"].get("exception_info"))
        self.assertEqual(sorted(f for f in os.listdir(job) if f.startswith("setup-")), ["setup-a"])
        self.assertEqual(self.drive_agent.call_count, 1)
        j = parse.parse_job(job)
        self.assertEqual({t.task_base: t.outcome for t in j.trials}, {"a": classify.SCORED, "b": classify.NO_PORT})
        self.assertEqual((j.counts[classify.NO_PORT], j.n_infra, j.mean_score), (1, 0, 0.25))
        with open(os.path.join(job, "result.json")) as f:
            stats = json.load(f)["stats"]
        self.assertEqual((stats["n_completed_trials"], stats["n_errored_trials"]), (2, 0))
        self.assertEqual(merge.result_of([{"result": {"n_total_trials": 2}, "trials": []}], list(trials.values()))["stats"]["n_errored_trials"], 0)
        tried = attempts.Attempts(tuple(t for t in j.trials if t.task_base == "b"))
        self.assertEqual((tried.has_verdicts, tried.rate), (False, 0.0))

    def test_on_a_pass_benchmark_it_stays_in_the_adjusted_rate(self):
        job, trials = self.go(self.agent_def(), kind="pass", reward=1.0)
        self.assertEqual(trials["b__u0001"]["verifier_result"]["rewards"], {"reward": 0.0})
        j = parse.parse_job(job)
        self.assertEqual((j.raw_solve_rate, j.adjusted_solve_rate), (0.5, 0.5))

    def test_repeated_it_counts_as_failed_attempts(self):
        job, trials = self.go(self.agent_def(), kind="pass", reward=1.0, repeat=2)
        self.assertEqual(sorted(t["standard"]["unit"] for t in trials.values()), ["a", "a", "b", "b"])
        with open(os.path.join(job, "result.json")) as f:
            hat = json.load(f)["pass_hat_k"]
        self.assertEqual((hat["units"], hat["1"]), (2, 0.5))
        self.assertEqual(attempts.for_job(parse.parse_job(job))["hat"][1], 0.5)

    def test_an_agent_with_the_port_and_the_null_control_run_it_as_before(self):
        control = definitions.control(BENCH, "nop")
        for other in (self.agent_def(proven=True), control):
            with self.subTest(other=other["key"]):
                job, trials = self.go(other)
                self.assertFalse(any(t.get("exception_info") for t in trials.values()))
                self.assertEqual(sorted(f for f in os.listdir(job) if f.startswith("setup-")),
                                 ["setup-a", "setup-b"])

    def test_the_live_feed_and_the_classifier_read_it_as_its_own(self):
        self.assertEqual(jobs.feed_outcome("score", {"reward": 0.0}, (jobs.NO_PORT, "not run"))["outcome"], "scored")
        self.assertEqual(jobs.feed_outcome("pass", {"reward": 0.0}, (jobs.NO_PORT, "not run"))["outcome"], "unresolved")
        self.assertEqual(classify.from_verdict(jobs.NO_PORT, None), classify.NO_PORT)
        self.assertNotIn(classify.NO_PORT, classify.INFRA_OUTCOMES)


class OSWorld2Units(unittest.TestCase):
    def test_the_simulated_user_and_multi_phase_tasks_need_the_conversation_port(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        driver = load_driver(tmp)
        with mock.patch.dict(os.environ, {"NULLIUS_DATASET": "", "NULLIUS_DATASET_ID": "websites98"}):
            listed = driver.units()
        wanted = [f"tasks__{e}" for e in "007 024 026 034 069 095 098".split() if f"tasks__{e}" in listed["units"]]
        self.assertEqual(sorted(listed["needs"]), wanted)
        self.assertTrue(all(v == [CONVERSATION] for v in listed["needs"].values()))
        with mock.patch.dict(os.environ, {"NULLIUS_DATASET": "", "NULLIUS_DATASET_ID": "offline36"}):
            self.assertEqual(driver.units()["needs"], {})
        meta = os.path.join(tmp, "meta.json")
        with open(meta, "w") as f:
            json.dump({"tasks": "001 007 024 026 034 069 095 098 108".split()}, f)
        with mock.patch.dict(os.environ, {"NULLIUS_DATASET": meta}):
            self.assertEqual(sorted(driver.units()["needs"]), [f"tasks__{e}" for e in "007 024 026 034 069 095 098".split()])


if __name__ == "__main__":
    unittest.main()
