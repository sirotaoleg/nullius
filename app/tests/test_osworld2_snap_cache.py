"""OSWorld2 task 093's snaps from this machine: the driver's copy checked against the store, its list given to the
Original, the null control and an agent's runner alike, and the guest's snapd download cache filled before the
upstream's setup. A fake store, a fake guest and a fake DesktopEnv stand in."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.test_osworld2_guest_disk import load_site
from tests.test_osworld2_write_limit import Started, load_driver

BLOB = b"snap file" * 1000
SHA = hashlib.sha3_384(BLOB).hexdigest()


def store(blob=BLOB, size=None, sha=SHA, asked=None):
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if asked is not None:
            asked.append(url)
        if "/v2/snaps/info/" in url:
            name = url.rsplit("/", 1)[1].split("?")[0]
            return io.BytesIO(json.dumps({"channel-map": [
                {"channel": {"architecture": "arm64", "track": "latest", "risk": "stable"}, "revision": 1,
                 "download": {"sha3-384": "x", "size": 1, "url": "https://cdn/arm"}},
                {"channel": {"architecture": "amd64", "track": "latest", "risk": "stable"}, "revision": 7,
                 "download": {"sha3-384": sha, "size": len(blob) if size is None else size,
                              "url": f"https://cdn/{name}"}}]}).encode())
        return io.BytesIO(blob)
    return urlopen


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        self.cache = os.path.join(self.tmp, "cache", "snaps")

    def test_093_s_snaps_are_kept_by_the_stores_digest_and_listed_once(self):
        asked = []
        with mock.patch.object(self.driver.urllib.request, "urlopen", store(asked=asked)):
            self.driver._snaps("093")
            self.driver._snaps("093")
        with open(os.path.join(self.cache, "093.json"), encoding="utf-8") as f:
            listed = json.load(f)
        self.assertEqual([s["name"] for s in listed], list(self.driver.SNAPS["093"]))
        self.assertEqual({(s["revision"], s["sha3_384"], s["size"], s["file"]) for s in listed},
                         {(7, SHA, len(BLOB), os.path.join(self.cache, SHA))})
        with open(os.path.join(self.cache, SHA), "rb") as f:
            self.assertEqual(f.read(), BLOB)
        self.assertEqual(sum(u.startswith("https://cdn/") for u in asked), 1)
        self.assertEqual(self.driver._snap_env("093"), {"OSWORLD2_SNAPS": os.path.join(self.cache, "093.json")})

    def test_a_file_unlike_the_stores_is_not_kept_and_the_unit_goes_on_without(self):
        for urlopen in (store(sha="0" * 96), store(size=len(BLOB) + 1)):
            with self.subTest(), mock.patch.object(self.driver.urllib.request, "urlopen", urlopen), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.driver._snaps("093")
            self.assertIn("the guest downloads them itself", err.getvalue())
            self.assertEqual(os.listdir(self.cache), [".lock"])
            self.assertEqual(self.driver._snap_env("093"), {})

    def test_no_store_leaves_the_unit_as_it_was(self):
        def down(*a, **kw):
            raise OSError("no route")

        with mock.patch.object(self.driver.urllib.request, "urlopen", down), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.driver._snaps("093")
        self.assertEqual(self.driver._snap_env("093"), {})

    def test_another_task_asks_the_store_nothing(self):
        with mock.patch.object(self.driver.urllib.request, "urlopen", side_effect=AssertionError("asked")):
            self.driver._snaps("045")
        os.makedirs(self.cache)
        open(os.path.join(self.cache, "045.json"), "w").close()
        self.assertEqual(self.driver._snap_env("045"), {})


class Paths(unittest.TestCase):
    """The list reaches the process of every path that starts a VM: the Original, the null control, an agent's runner."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(self.unit)
        self.driver = load_driver(self.tmp)
        self.driver.START_LOCK = os.path.join(self.tmp, "start.lck")
        self.envs = []

        def started(argv, dir, env, *a, **kw):
            self.envs.append(env)
            raise Started()

        def popen(argv, cwd=None, env=None, **kw):
            self.envs.append(env)
            raise Started()

        for target, name, value in ((self.driver, "vm_image", lambda: "image"), (self.driver, "_task_file", lambda *a: None),
                                    (self.driver, "_provider_image", lambda: None), (self.driver, "_write_limit", lambda: None),
                                    (self.driver, "_nat", lambda: True), (self.driver, "ASSETS", self.tmp),
                                    (self.driver, "_services", lambda *a, **kw: {}), (self.driver, "_assets", lambda *a: "assets"),
                                    (self.driver, "_task", lambda *a: {"instruction": ""}), (self.driver, "_run", started),
                                    (self.driver.subprocess, "Popen", popen),
                                    (self.driver.urllib.request, "urlopen", store()),
                                    (self.driver._startup(), "network", lambda docker: "nullius-osworld2")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"NULLIUS_OPTION_WRITE_MBPS": "24", "NULLIUS_OPTION_WRITE_IOPS": "240",
                                         "NULLIUS_OPTION_VM_PROVIDER": "docker", "NULLIUS_OPTION_AGENT": "m3"})
        p.start()
        self.addCleanup(p.stop)

    def start(self, eid):
        judge = {"model": "judge", "base_url": "http://127.0.0.1:9/v1", "key": "k"}
        for name, run in (("original", lambda: self.driver.answer(f"tasks__{eid}", self.unit, "original", "MiniMax-M3",
                                                                   "http://127.0.0.1:9/u1/v1", "k", judge=judge)),
                          ("nop", lambda: self.driver.score(f"tasks__{eid}", self.unit, judge=judge)),
                          ("agent", lambda: self.driver.setup(f"tasks__{eid}", self.unit, participant="agent"))):
            with self.subTest(name), self.assertRaises(Started):
                run()
        return self.envs

    def test_093_s_list_reaches_all_three(self):
        envs = self.start("093")
        self.assertEqual([e.get("OSWORLD2_SNAPS") for e in envs],
                         [os.path.join(self.tmp, "cache", "snaps", "093.json")] * 3)

    def test_another_tasks_reaches_none(self):
        self.assertEqual([e.get("OSWORLD2_SNAPS") for e in self.start("045")], [None] * 3)


class Guest:
    """The guest's control server: a command's return code from what is installed and what snapd linked."""
    def __init__(self, installed=(), hit=()):
        self.installed, self.hit, self.said = set(installed), set(hit), []

    def post(self, url, json=None, timeout=None):
        command = json["command"]
        self.said.append(command)
        rc = 0
        if command.startswith("snap list "):
            rc = 0 if command.split()[-1] in self.installed else 1
        elif "stat -c %i" in command:
            rc = 0 if any(f"/var/lib/snapd/snaps/{n}_" in command for n in self.hit) else 1
        return types.SimpleNamespace(status_code=200, json=lambda: {"returncode": rc, "output": ""})


class Controller:
    http_server = "http://127.0.0.1:5000"
    client_password = "pw"

    def __init__(self, events):
        self.events = events

    def ensure_ready(self, use_proxy=False):
        self.events.append(("ready", use_proxy))
        return True

    def _upload_file_setup(self, files):
        self.events.append(("upload", files[0]["local_path"], files[0]["path"]))


class Seed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.site = load_site()
        self.events = []
        events = self.events

        class DesktopEnv:
            def __init__(self):
                self.setup_controller = Controller(events)

            def _setup_task(self, task_config, use_proxy):
                events.append(("setup", task_config))
                return True, True

        self.DesktopEnv = DesktopEnv
        self.site.PATCHES[self.site.DESKTOP_ENV](types.SimpleNamespace(DesktopEnv=DesktopEnv))
        self.files = {}
        for name in ("openboard", "core24", "kf6-core24"):
            path = os.path.join(self.tmp, name)
            open(path, "w").close()
            self.files[name] = path
        listed = [{"name": n, "revision": 7, "sha3_384": f"{n}-sha", "size": 1, "file": p} for n, p in self.files.items()]
        listed.append({"name": "gone", "revision": 1, "sha3_384": "gone-sha", "size": 1, "file": os.path.join(self.tmp, "x")})
        self.listed = os.path.join(self.tmp, "093.json")
        with open(self.listed, "w") as f:
            json.dump(listed, f)

    def run_setup(self, guest, environ):
        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                mock.patch.dict(os.environ, environ, clear=False), self.assertLogs("nullius.osworld2", "INFO") as logs:
            out = self.DesktopEnv()._setup_task("task", True)
        return out, [r.getMessage() for r in logs.records]

    def test_the_guests_missing_snaps_go_to_snapds_cache_before_the_setup_then_hit_or_miss(self):
        guest = Guest(installed={"core24"}, hit={"openboard"})
        out, said = self.run_setup(guest, {"OSWORLD2_SNAPS": self.listed})
        self.assertEqual(out, (True, True))
        self.assertEqual(self.events, [("ready", True),
                                       ("upload", self.files["openboard"], "/tmp/nullius-snaps/openboard-sha"),
                                       ("upload", self.files["kf6-core24"], "/tmp/nullius-snaps/kf6-core24-sha"),
                                       ("setup", "task")])
        moved = [c for c in guest.said if "mv /tmp/nullius-snaps/" in c]
        self.assertEqual(len(moved), 2)
        self.assertIn("mv /tmp/nullius-snaps/openboard-sha /var/lib/snapd/cache/openboard-sha", moved[0])
        self.assertTrue(all(c.startswith("echo 'pw' | sudo -S sh -c '") for c in moved))
        self.assertEqual(said, ["snap cache openboard: hit", "snap cache kf6-core24: miss"])

    def test_with_no_list_the_setup_is_the_upstreams_alone(self):
        guest = Guest()
        os.environ.pop("OSWORLD2_SNAPS", None)
        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}):
            self.assertEqual(self.DesktopEnv()._setup_task("task", False), (True, True))
        self.assertEqual((self.events, guest.said), ([("setup", "task")], []))

    def test_a_guest_that_fails_leaves_the_setup_to_the_store(self):
        def post(*a, **kw):
            raise ConnectionError("guest")

        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=post)}), \
                mock.patch.dict(os.environ, {"OSWORLD2_SNAPS": self.listed}), \
                self.assertLogs("nullius.osworld2", "WARNING") as logs:
            self.assertEqual(self.DesktopEnv()._setup_task("task", False), (True, True))
        self.assertEqual(self.events[-1], ("setup", "task"))
        self.assertEqual([r.getMessage() for r in logs.records], ["snap cache not used: ConnectionError"])

    def test_the_setup_is_wrapped_once(self):
        wrapped = self.DesktopEnv._setup_task
        self.site.PATCHES[self.site.DESKTOP_ENV](types.SimpleNamespace(DesktopEnv=self.DesktopEnv))
        self.assertIs(self.DesktopEnv._setup_task, wrapped)


if __name__ == "__main__":
    unittest.main()
