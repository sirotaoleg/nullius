"""OSWorld2 task 093's snaps from this machine: the driver's copy checked against the store and served on the VMs'
gateway for the unit's life to the Original, the null control and an agent's runner alike, and the guest's own fetch
into snapd's download cache, checked, before the upstream's setup. A fake store, guest and DesktopEnv stand in."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.test_osworld2_guest_disk import load_site
from tests.test_osworld2_write_limit import Started, load_driver

BLOB = b"snap file" * 1000
SHA = hashlib.sha3_384(BLOB).hexdigest()


class Answer(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status


def store(blob=BLOB, size=None, sha=SHA, asked=None, ranges=True):
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if asked is not None:
            asked.append(url if "/v2/snaps/info/" in url or not getattr(req, "get_header", None) or not
                         req.get_header("Range") else f"{url} {req.get_header('Range')}")
        if "/v2/snaps/info/" in url:
            name = url.rsplit("/", 1)[1].split("?")[0]
            return Answer(json.dumps({"channel-map": [
                {"channel": {"architecture": "arm64", "track": "latest", "risk": "stable"}, "revision": 1,
                 "download": {"sha3-384": "x", "size": 1, "url": "https://cdn/arm"}},
                {"channel": {"architecture": "amd64", "track": "latest", "risk": "stable"}, "revision": 7,
                 "download": {"sha3-384": sha, "size": len(blob) if size is None else size,
                              "url": f"https://cdn/{name}"}}]}).encode())
        wanted = req.get_header("Range") if hasattr(req, "get_header") else None
        if wanted and ranges:
            return Answer(blob[int(wanted[len("bytes="):-1]):], 206)
        return Answer(blob)
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
        self.assertTrue(self.listed())

    def test_a_file_unlike_the_stores_is_not_kept_and_the_unit_goes_on_without(self):
        for urlopen in (store(sha="0" * 96), store(size=len(BLOB) + 1)):
            with self.subTest(), mock.patch.object(self.driver.urllib.request, "urlopen", urlopen), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.driver._snaps("093")
            self.assertIn("the guest downloads them itself", err.getvalue())
            self.assertEqual(os.listdir(self.cache), [".lock"])
            self.assertFalse(self.listed())

    def test_no_store_leaves_the_unit_as_it_was(self):
        def down(*a, **kw):
            raise OSError("no route")

        with mock.patch.object(self.driver.urllib.request, "urlopen", down), \
                mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch.object(self.driver.time, "sleep") as slept:
            self.driver._snaps("093")
        self.assertEqual(slept.call_count, self.driver.SNAP_TRIES - 1)
        self.assertFalse(self.listed())

    def test_a_dropped_connection_is_tried_again(self):
        good, dropped = store(), []

        def flaky(req, timeout=None):
            if len(dropped) < 2:
                dropped.append(req)
                raise OSError("EOF")
            return good(req, timeout)

        with mock.patch.object(self.driver.urllib.request, "urlopen", flaky), \
                mock.patch.object(self.driver.time, "sleep") as slept:
            self.driver._snaps("093")
        self.assertEqual((len(dropped), slept.call_count), (2, 2))
        self.assertTrue(self.listed())

    def test_a_part_left_by_an_earlier_unit_is_gone_on_from_or_made_again(self):
        for ranges in (True, False):
            with self.subTest(ranges=ranges):
                shutil.rmtree(self.cache, ignore_errors=True)
                os.makedirs(self.cache)
                with open(os.path.join(self.cache, SHA + ".part"), "wb") as f:
                    f.write(BLOB[:len(BLOB) // 2])
                asked = []
                with mock.patch.object(self.driver.urllib.request, "urlopen", store(ranges=ranges, asked=asked)):
                    self.driver._snaps("093")
                self.assertEqual([u for u in asked if u.startswith("https://cdn/")],
                                 [f"https://cdn/openboard bytes={len(BLOB) // 2}-"])
                with open(os.path.join(self.cache, SHA), "rb") as f:
                    self.assertEqual(f.read(), BLOB)
                self.assertFalse(os.path.exists(os.path.join(self.cache, SHA + ".part")))

    def test_another_task_asks_the_store_nothing(self):
        with mock.patch.object(self.driver.urllib.request, "urlopen", side_effect=AssertionError("asked")):
            self.driver._snaps("045")
        os.makedirs(self.cache)
        open(os.path.join(self.cache, "045.json"), "w").close()
        with self.driver._snap_served("045") as served:
            self.assertEqual(served, {})

    def listed(self):
        return os.path.exists(os.path.join(self.cache, "093.json"))


def get(url, urlopen=urllib.request.urlopen):
    try:
        with urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, b""
    except OSError as e:
        return type(e).__name__, b""


class Served(unittest.TestCase):
    """The driver's server: the listed files by their sha3-384 alone, on the gateway only, for the unit's life."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        p = mock.patch.object(self.driver._startup(), "GATEWAY", "127.0.0.1")
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(self.driver.urllib.request, "urlopen", store()):
            self.driver._snaps("093")
        self.cache = os.path.join(self.tmp, "cache", "snaps")
        with open(os.path.join(self.cache, "other"), "w") as f:
            f.write("not listed")

    def test_only_listed_files_by_digest_and_closed_however_the_unit_ends(self):
        for ends in ("done", "failed"):
            with self.subTest(ends):
                try:
                    with self.driver._snap_served("093") as served:
                        url = served["OSWORLD2_SNAP_URL"]
                        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+$")
                        self.assertEqual(served["OSWORLD2_SNAPS"], os.path.join(self.cache, "093.json"))
                        self.assertEqual(get(f"{url}/{SHA}"), (200, BLOB))
                        for other in ("", "/", "/other", "/093.json", f"/{SHA}/..", "/.lock", f"/../{SHA}"):
                            self.assertEqual(get(f"{url}{other}")[0], 404, other)
                        if ends == "failed":
                            raise Started()
                except Started:
                    pass
                self.assertEqual(get(f"{url}/{SHA}")[0], "URLError")

    def test_no_list_or_no_gateway_serves_nothing(self):
        os.remove(os.path.join(self.cache, "093.json"))
        with self.driver._snap_served("093") as served:
            self.assertEqual(served, {})
        with mock.patch.object(self.driver.urllib.request, "urlopen", store()):
            self.driver._snaps("093")
        with mock.patch.object(self.driver._startup(), "GATEWAY", "192.0.2.1"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err, self.driver._snap_served("093") as served:
            self.assertEqual(served, {})
        self.assertIn("not served to the guest", err.getvalue())


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
            self.envs.append((env, get(f"{env.get('OSWORLD2_SNAP_URL')}/{SHA}")[0]))
            raise Started()

        def popen(argv, cwd=None, env=None, **kw):
            self.envs.append((env, get(f"{env.get('OSWORLD2_SNAP_URL')}/{SHA}")[0]))
            raise Started()

        nothing = lambda *a, **kw: contextlib.nullcontext({})

        for target, name, value in ((self.driver, "vm_image", lambda: "image"), (self.driver, "_task_file", lambda *a: None),
                                    (self.driver, "_provider_image", lambda: None), (self.driver, "_write_limit", lambda: None),
                                    (self.driver, "_nat", lambda: True), (self.driver, "ASSETS", self.tmp),
                                    (self.driver, "_published", nothing), (self.driver.websites, "held", nothing),
                                    (self.driver, "_gitlab", nothing), (self.driver.proxy, "held", nothing),
                                    (self.driver._startup(), "GATEWAY", "127.0.0.1"), (self.driver, "_assets", lambda *a: "assets"),
                                    (self.driver, "_task", lambda *a: {"instruction": "", "proxy": False}),
                                    (self.driver, "_run", started),
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

    def test_093_s_snaps_are_served_to_all_three_while_their_process_runs_and_not_after(self):
        envs = self.start("093")
        self.assertEqual([(e.get("OSWORLD2_SNAPS"), got) for e, got in envs],
                         [(os.path.join(self.tmp, "cache", "snaps", "093.json"), 200)] * 3)
        self.assertEqual([get(f"{e['OSWORLD2_SNAP_URL']}/{SHA}")[0] for e, _ in envs], ["URLError"] * 3)

    def test_another_tasks_gets_none(self):
        self.assertEqual([(e.get("OSWORLD2_SNAPS"), e.get("OSWORLD2_SNAP_URL")) for e, _ in self.start("045")],
                         [(None, None)] * 3)


class Guest:
    """The guest's control server: a command's return code from what is installed, delivered and linked by snapd."""
    def __init__(self, events, installed=(), undelivered=(), hit=()):
        self.events, self.installed, self.undelivered, self.hit = events, set(installed), set(undelivered), set(hit)

    def post(self, url, json=None, timeout=None):
        command = json["command"]
        self.events.append(("guest", command, json.get("timeout"), timeout))
        rc = 0
        if command.startswith("snap list "):
            rc = 0 if command.split()[-1] in self.installed else 1
        elif "curl -fsS" in command:
            rc = 1 if any(f"/{n}-sha " in command for n in self.undelivered) else 0
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
        listed = [{"name": n, "revision": 7, "sha3_384": f"{n}-sha", "size": 11, "file": os.path.join(self.tmp, n)}
                  for n in ("openboard", "core24", "kf6-core24", "mesa-2404")]
        self.listed = os.path.join(self.tmp, "093.json")
        with open(self.listed, "w") as f:
            json.dump(listed, f)
        self.given = {"OSWORLD2_SNAPS": self.listed, "OSWORLD2_SNAP_URL": "http://10.213.0.1:4242"}

    def run_setup(self, guest, environ):
        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                mock.patch.dict(os.environ, environ), self.assertLogs("nullius.osworld2", "INFO") as logs:
            out = self.DesktopEnv()._setup_task("task", True)
        return out, [r.getMessage() for r in logs.records]

    def test_the_guest_fetches_what_it_lacks_into_snapds_cache_checked_before_the_setup_then_hit_or_miss(self):
        guest = Guest(self.events, installed={"core24"}, undelivered={"mesa-2404"}, hit={"openboard"})
        out, said = self.run_setup(guest, self.given)
        self.assertEqual(out, (True, True))
        fetched = [e for e in self.events if e[0] == "guest" and "curl -fsS" in e[1]]
        self.assertEqual(len(fetched), 3)
        self.assertLess(self.events.index(fetched[-1]), self.events.index(("setup", "task")))
        first = fetched[0][1]
        self.assertTrue(first.startswith("echo 'pw' | sudo -S sh -c '"))
        for part in ("http://10.213.0.1:4242/openboard-sha", "/var/lib/snapd/cache/openboard-sha.part",
                     'test "$(stat -c %s /var/lib/snapd/cache/openboard-sha.part)" = 11',
                     'openssl dgst -sha3-384 -r /var/lib/snapd/cache/openboard-sha.part',
                     '= openboard-sha && mv /var/lib/snapd/cache/openboard-sha.part /var/lib/snapd/cache/openboard-sha',
                     "|| { rm -f /var/lib/snapd/cache/openboard-sha.part; exit 1; }"):
            self.assertIn(part, first)
        self.assertEqual(fetched[0][2:], (self.site.SNAP_SECONDS + 120, self.site.SNAP_SECONDS + 150))
        self.assertEqual(said, ["snap cache mesa-2404: not delivered", "snap cache openboard: hit",
                                "snap cache kf6-core24: miss"])

    def test_without_the_list_or_the_address_the_setup_is_the_upstreams_alone(self):
        for environ in ({"OSWORLD2_SNAPS": self.listed}, {"OSWORLD2_SNAP_URL": "http://10.213.0.1:4242"}):
            with self.subTest(environ):
                self.events.clear()
                guest = Guest(self.events)
                with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                        mock.patch.dict(os.environ, environ):
                    os.environ.pop("OSWORLD2_SNAPS" if "OSWORLD2_SNAPS" not in environ else "OSWORLD2_SNAP_URL", None)
                    self.assertEqual(self.DesktopEnv()._setup_task("task", False), (True, True))
                self.assertEqual(self.events, [("setup", "task")])

    def test_a_guest_that_fails_leaves_the_setup_to_the_store(self):
        def post(*a, **kw):
            raise ConnectionError("guest")

        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=post)}), \
                mock.patch.dict(os.environ, self.given), self.assertLogs("nullius.osworld2", "WARNING") as logs:
            self.assertEqual(self.DesktopEnv()._setup_task("task", False), (True, True))
        self.assertEqual(self.events[-1], ("setup", "task"))
        self.assertEqual([r.getMessage() for r in logs.records], ["snap cache not used: ConnectionError"])

    def test_the_setup_is_wrapped_once(self):
        wrapped = self.DesktopEnv._setup_task
        self.site.PATCHES[self.site.DESKTOP_ENV](types.SimpleNamespace(DesktopEnv=self.DesktopEnv))
        self.assertIs(self.DesktopEnv._setup_task, wrapped)


if __name__ == "__main__":
    unittest.main()
