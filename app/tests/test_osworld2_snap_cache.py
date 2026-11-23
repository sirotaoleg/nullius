"""OSWorld2 task 093's snaps from this machine: the driver's copy of the store's current revisions, filled in the
background and checked, served on the VMs' gateway for the unit's life to the Original, the null control and an
agent's runner alike, and the guest's own fetch into snapd's download cache, checked, before the upstream's setup.
A fake store, guest and DesktopEnv stand in; the guest's command runs in a temporary folder."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.test_osworld2_guest_disk import load_site
from tests.test_osworld2_write_limit import Started, load_driver

BLOB = b"snap file" * 1000
SHA = hashlib.sha3_384(BLOB).hexdigest()
OLD = hashlib.sha3_384(b"old").hexdigest()
BIG = hashlib.sha3_384(b"big").hexdigest()
BIG_SIZE = 64 << 20
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Answer(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status


class Store:
    """The Snap Store and its CDN: every name's latest/stable amd64 revision is ``blob``; ``short`` answers end early."""
    def __init__(self, blob=BLOB, sha=SHA, size=None, ranges=True, short=0, dropped=0, down=False):
        self.blob, self.sha, self.size, self.ranges = blob, sha, len(blob) if size is None else size, ranges
        self.short, self.dropped, self.down, self.asked = short, dropped, down, []
        self.listed, self.listed_before = None, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        wanted = req.get_header("Range")
        self.asked.append(f"{url} {wanted}" if wanted else url)
        if self.down:
            raise OSError("no route")
        if self.dropped:
            self.dropped -= 1
            raise OSError("EOF")
        if "/v2/snaps/info/" in url:
            name = url.rsplit("/", 1)[1].split("?")[0]
            return Answer(json.dumps({"channel-map": [
                {"channel": {"architecture": "arm64", "track": "latest", "risk": "stable"}, "revision": 1,
                 "download": {"sha3-384": OLD, "size": 1, "url": "https://cdn/arm"}},
                {"channel": {"architecture": "amd64", "track": "latest", "risk": "stable"}, "revision": 7,
                 "download": {"sha3-384": self.sha, "size": self.size, "url": f"https://cdn/{name}"}}]}).encode())
        if self.listed is not None:
            self.listed_before.append(self.listed.is_set())
        start = int(wanted[len("bytes="):-1]) if wanted and self.ranges else 0
        body = self.blob[start:]
        if self.short:
            self.short -= 1
            body = body[:len(body) // 2]
        return Answer(body, 206 if start else 200)


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        self.cache = os.path.join(self.tmp, "cache", "snaps")
        p = mock.patch.object(self.driver.time, "sleep")
        self.slept = p.start()
        self.addCleanup(p.stop)

    def fill(self, store, eid="093", fresh=True):
        listed = store.listed = threading.Event()
        with mock.patch.object(self.driver.urllib.request, "urlopen", store), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            done = threading.Thread(target=self.driver._snap_fill, args=(eid, listed))
            done.start()
            done.join(20)
        self.assertFalse(done.is_alive(), "the filling waited")
        self.assertEqual(listed.is_set(), fresh)
        return err.getvalue()

    def listed(self):
        with open(os.path.join(self.cache, "093.json"), encoding="utf-8") as f:
            return json.load(f)

    def files(self):
        return sorted(n for n in os.listdir(self.cache) if not n.startswith("."))

    def test_the_current_revisions_are_kept_by_the_stores_digest_and_listed(self):
        store = Store()
        self.assertEqual(self.fill(store), "")
        self.assertEqual([(s["name"], s["revision"], s["sha3_384"], s["size"]) for s in self.listed()],
                         [(n, 7, SHA, len(BLOB)) for n in self.driver.SNAPS["093"]])
        with open(os.path.join(self.cache, SHA), "rb") as f:
            self.assertEqual(f.read(), BLOB)
        self.assertEqual(sum(u.startswith("https://cdn/") for u in store.asked), 1)
        self.assertEqual(store.listed_before, [True])
        self.fill(store)
        self.assertEqual(sum(u.startswith("https://cdn/") for u in store.asked), 1)

    def test_a_file_unlike_the_stores_is_not_kept_or_listed(self):
        for store in (Store(sha="0" * 96), Store(size=len(BLOB) - 1)):
            with self.subTest():
                self.assertIn("the guest downloads them itself", self.fill(store))
                self.assertEqual((self.files(), self.listed()), (["093.json"], []))

    def test_an_answer_that_ends_early_is_tried_again_from_where_it_ended(self):
        store = Store(short=1)
        self.assertEqual(self.fill(store), "")
        self.assertEqual([u for u in store.asked if u.startswith("https://cdn/")],
                         ["https://cdn/openboard", f"https://cdn/openboard bytes={len(BLOB) // 2}-"])
        self.assertEqual(len(self.listed()), 3)

    def test_a_part_left_whole_is_checked_without_asking_and_one_too_long_is_made_again(self):
        for had, asked in ((BLOB, []), (BLOB + b"x", ["https://cdn/openboard"]),
                           (BLOB[:100], ["https://cdn/openboard bytes=100-"])):
            with self.subTest(len(had)):
                shutil.rmtree(self.cache, ignore_errors=True)
                os.makedirs(self.cache)
                with open(os.path.join(self.cache, SHA + ".part"), "wb") as f:
                    f.write(had)
                store = Store()
                self.fill(store)
                self.assertEqual([u for u in store.asked if u.startswith("https://cdn/")], asked)
                self.assertEqual(self.files(), ["093.json", SHA])

    def test_a_store_that_ignores_the_range_gives_the_whole_file_again(self):
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, SHA + ".part"), "wb") as f:
            f.write(BLOB[:100])
        self.fill(Store(ranges=False))
        with open(os.path.join(self.cache, SHA), "rb") as f:
            self.assertEqual(f.read(), BLOB)

    def test_a_dropped_connection_is_tried_again_and_no_store_lists_nothing(self):
        self.assertEqual(self.fill(Store(dropped=2)), "")
        self.assertEqual(len(self.listed()), 3)
        self.slept.reset_mock()
        shutil.rmtree(self.cache)
        self.assertIn("OSError", self.fill(Store(down=True), fresh=False))
        self.assertEqual(self.slept.call_count, self.driver.SNAP_TRIES - 1)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "093.json")))

    def test_a_revision_the_store_no_longer_names_is_not_listed_even_before_the_new_one_is_here(self):
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, OLD), "wb") as f:
            f.write(b"old")
        with open(os.path.join(self.cache, "093.json"), "w") as f:
            json.dump([{"name": "openboard", "revision": 6, "sha3_384": OLD, "size": 3}], f)
        self.fill(Store(short=99))
        self.assertEqual(self.listed(), [])

    def test_a_cache_another_driver_fills_is_left_to_it(self):
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, ".lock"), "w") as held:
            self.driver.fcntl.flock(held, self.driver.fcntl.LOCK_EX)
            store = Store()
            self.assertEqual(self.fill(store, fresh=False), "")
        self.assertEqual(store.asked, [])

    def test_the_unit_starts_one_filling_and_another_task_none(self):
        started = []
        with mock.patch.object(self.driver.threading, "Thread",
                               lambda target, args, daemon: types.SimpleNamespace(start=lambda: started.append(args))):
            for eid in ("093", "093", "045"):
                self.driver._snaps(eid)
        self.assertEqual([a[0] for a in started], ["093"])
        self.assertEqual((self.driver._FILLING, set(self.driver._FRESH)), ({"093"}, {"093"}))
        self.driver._FILLING.clear()
        self.driver._snaps("093")
        self.assertEqual([a[0] for a in started], ["093"])


def get(url):
    try:
        with DIRECT.open(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, b""
    except (OSError, ValueError) as e:
        return type(e).__name__, b""


class Served(unittest.TestCase):
    """The driver's server: the cache's listed files by their sha3-384 alone, on the gateway, closed with the unit."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = load_driver(self.tmp)
        p = mock.patch.object(self.driver._startup(), "GATEWAY", "127.0.0.1")
        p.start()
        self.addCleanup(p.stop)
        self.cache = os.path.join(self.tmp, "cache", "snaps")
        os.makedirs(self.cache)
        for name, data in ((SHA, BLOB), ("other", b"not listed"), (OLD, b"old")):
            with open(os.path.join(self.cache, name), "wb") as f:
                f.write(data)
        with open(os.path.join(self.cache, BIG), "wb") as f:
            f.truncate(BIG_SIZE)
        with open(os.path.join(self.tmp, "secret"), "w") as f:
            f.write("secret")
        self.list([{"name": "openboard", "revision": 7, "sha3_384": SHA, "size": len(BLOB),
                    "file": os.path.join(self.tmp, "secret")},
                   {"name": "kf6-core24", "revision": 69, "sha3_384": BIG, "size": BIG_SIZE},
                   {"name": "bad", "revision": 1, "sha3_384": "../other", "size": 1},
                   {"name": "x; reboot", "revision": 1, "sha3_384": OLD, "size": 3},
                   {"name": "lxqt-support-core24", "revision": 19, "sha3_384": "x; reboot", "size": 1}])
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(self.unit)
        fresh = threading.Event()
        fresh.set()
        self.driver._FRESH["093"] = fresh

    def list(self, snaps):
        with open(os.path.join(self.cache, "093.json"), "w") as f:
            json.dump(snaps, f)

    def test_only_listed_files_by_digest_from_the_cache_and_a_list_for_the_guest(self):
        with self.driver._snap_served("093", self.unit) as served:
            url = served["OSWORLD2_SNAP_URL"]
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+$")
            self.assertEqual(get(f"{url}/{SHA}"), (200, BLOB))
            for other in ("", "/", "/other", "/093.json", f"/{OLD}", "/../secret", f"/{SHA}/..", "/.lock", "/bad"):
                self.assertEqual(get(f"{url}{other}")[0], 404, other)
            with open(served["OSWORLD2_SNAPS"]) as f:
                self.assertEqual([s["sha3_384"] for s in json.load(f)], [SHA, BIG])
            self.assertEqual(os.path.dirname(served["OSWORLD2_SNAPS"]), self.unit)

    def test_every_connection_is_closed_however_the_unit_ends(self):
        for ends in ("done", "failed"):
            with self.subTest(ends):
                try:
                    with self.driver._snap_served("093", self.unit) as served:
                        url = served["OSWORLD2_SNAP_URL"]
                        host, port = url[len("http://"):].split(":")
                        idle = socket.create_connection((host, int(port)), timeout=5)
                        idle.sendall(b"GET /")
                        midway = socket.socket()
                        midway.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                        midway.settimeout(5)
                        midway.connect((host, int(port)))
                        midway.sendall(f"GET /{BIG} HTTP/1.0\r\n\r\n".encode())
                        midway.recv(10)
                        if ends == "failed":
                            raise Started()
                except Started:
                    pass
                self.assertEqual(get(f"{url}/{SHA}")[0], "URLError")
                for s in (idle, midway):
                    got = b""
                    with contextlib.suppress(OSError):
                        while len(got) < BIG_SIZE // 2 and (chunk := s.recv(1 << 16)):
                            got += chunk
                    s.close()
                    self.assertLess(len(got), BIG_SIZE // 2)

    def test_an_idle_connection_is_dropped(self):
        with mock.patch.object(self.driver, "SNAP_CONNECTION_SECONDS", 1), \
                self.driver._snap_served("093", self.unit) as served:
            host, port = served["OSWORLD2_SNAP_URL"][len("http://"):].split(":")
            idle = socket.create_connection((host, int(port)), timeout=10)
            idle.sendall(b"GET /")
            started = time.time()
            with contextlib.suppress(OSError):
                while idle.recv(1 << 16):
                    pass
            idle.close()
            self.assertLess(time.time() - started, 5)
            self.assertEqual(get(f"{served['OSWORLD2_SNAP_URL']}/{SHA}")[0], 200)

    def test_connections_past_the_limit_are_closed_at_once(self):
        with self.driver._snap_served("093", self.unit) as served:
            host, port = served["OSWORLD2_SNAP_URL"][len("http://"):].split(":")
            held = [socket.create_connection((host, int(port)), timeout=5) for _ in range(self.driver.SNAP_CONNECTIONS)]
            for h in held:
                h.sendall(b"GET /")
            time.sleep(0.5)
            extra = socket.create_connection((host, int(port)), timeout=5)
            started = time.time()
            with contextlib.suppress(OSError):
                self.assertEqual(extra.recv(10), b"")
            self.assertLess(time.time() - started, 3)
            for h in held + [extra]:
                h.close()

    def test_a_list_not_written_for_the_guest_serves_nothing(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                self.driver._snap_served("093", os.path.join(self.tmp, "no", "such")) as served:
            self.assertEqual(served, {})
        self.assertIn("not served to the guest", err.getvalue())

    def test_no_list_or_no_gateway_serves_nothing(self):
        for snaps in ([], [{"name": "bad", "revision": 1, "sha3_384": "x", "size": 1}]):
            self.list(snaps)
            with self.driver._snap_served("093", self.unit) as served:
                self.assertEqual(served, {})
        self.list([{"name": "openboard", "revision": 7, "sha3_384": SHA, "size": len(BLOB)}])
        with mock.patch.object(self.driver._startup(), "GATEWAY", "192.0.2.1"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                self.driver._snap_served("093", self.unit) as served:
            self.assertEqual(served, {})
        self.assertIn("not served to the guest", err.getvalue())
        with self.driver._snap_served("045", self.unit) as served:
            self.assertEqual(served, {})

    def test_only_a_list_made_against_the_store_in_time_is_served(self):
        event = threading.Event()
        self.driver._FRESH["093"] = event
        for ready in (False, True):
            with self.subTest(ready=ready), mock.patch.object(event, "wait", return_value=ready) as waited, \
                    self.driver._snap_served("093", self.unit) as served:
                self.assertEqual(bool(served), ready)
            waited.assert_called_once_with(self.driver.SNAP_LISTED_SECONDS)
        del self.driver._FRESH["093"]
        with self.driver._snap_served("093", self.unit) as served:
            self.assertEqual(served, {})


class Paths(unittest.TestCase):
    """The list and the address reach the process of every path that starts a VM: the Original, the null control,
    an agent's runner; the server answers while that process starts and is closed after."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.unit = os.path.join(self.tmp, "unit")
        os.makedirs(self.unit)
        self.driver = load_driver(self.tmp)
        self.driver.START_LOCK = os.path.join(self.tmp, "start.lck")
        self.envs = []
        self.store = Store()
        cache = os.path.join(self.tmp, "cache", "snaps")
        os.makedirs(cache)
        with open(os.path.join(cache, SHA), "wb") as f:
            f.write(BLOB)
        with open(os.path.join(cache, "093.json"), "w") as f:
            json.dump([{"name": "openboard", "revision": 7, "sha3_384": SHA, "size": len(BLOB)}], f)

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
                                    (self.driver, "_run", started), (self.driver.subprocess, "Popen", popen),
                                    (self.driver.urllib.request, "urlopen", self.store),
                                    (self.driver.time, "sleep", lambda s: None), (self.driver, "SNAP_LISTED_SECONDS", 10),
                                    (self.driver._startup(), "network", lambda docker: "nullius-osworld2")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"NULLIUS_OPTION_WRITE_MBPS": "24", "NULLIUS_OPTION_WRITE_IOPS": "240",
                                         "NULLIUS_OPTION_VM_PROVIDER": "docker", "NULLIUS_OPTION_AGENT": "m3"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("sys.stderr", new_callable=io.StringIO)
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

    def test_093_s_snaps_are_served_to_all_three_while_their_process_starts_and_not_after(self):
        envs = self.start("093")
        self.assertEqual([got for _, got in envs], [200] * 3)
        for env, _ in envs:
            with open(env["OSWORLD2_SNAPS"]) as f:
                self.assertEqual({(s["name"], s["sha3_384"]) for s in json.load(f)},
                                 {(n, SHA) for n in self.driver.SNAPS["093"]})
        self.assertEqual([get(f"{e['OSWORLD2_SNAP_URL']}/{SHA}")[0] for e, _ in envs], ["URLError"] * 3)
        for _ in range(100):
            if not self.driver._FILLING:
                break
            time.sleep(0.05)
        self.assertFalse(self.driver._FILLING)
        self.assertTrue(any("/v2/snaps/info/" in u for u in self.store.asked))

    def test_another_tasks_gets_none(self):
        self.assertEqual([(e.get("OSWORLD2_SNAPS"), e.get("OSWORLD2_SNAP_URL")) for e, _ in self.start("045")],
                         [(None, None)] * 3)


WGET = """#!/bin/sh
out=""
while [ $# -gt 0 ]; do
  case "$1" in -O) out="$2"; shift 2;; *) shift;; esac
done
case "$FAKE_WGET" in fail) exit 4;; slow) sleep 30;; esac
cp "$FAKE_SOURCE" "$out"
"""


class Fetch(unittest.TestCase):
    """The guest's command, run here: wget stands in; timeout, stat and openssl are this machine's own."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.site = load_site()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "wget"), "w") as f:
            f.write(WGET)
        os.chmod(os.path.join(bin_dir, "wget"), 0o755)
        self.source = os.path.join(self.tmp, "source")
        self.cache = os.path.join(self.tmp, "cache")
        self.path = bin_dir + os.pathsep + os.environ["PATH"]

    def run_fetch(self, data, wget="", size=None, seconds=20):
        with open(self.source, "wb") as f:
            f.write(data)
        snap = {"sha3_384": SHA, "size": len(BLOB) if size is None else size}
        command = self.site._fetch(snap, "http://10.213.0.1:4242", seconds, cache=self.cache)
        done = subprocess.run(["sh", "-c", command], env=dict(os.environ, PATH=self.path, FAKE_SOURCE=self.source,
                                                              FAKE_WGET=wget), capture_output=True, timeout=60)
        return done.returncode, sorted(os.listdir(self.cache))

    def test_the_stores_file_is_kept_by_its_digest(self):
        self.assertEqual(self.run_fetch(BLOB), (0, [SHA]))
        self.assertEqual(oct(os.stat(os.path.join(self.cache, SHA)).st_mode & 0o777), "0o644")

    def test_another_file_a_failed_or_a_slow_download_leaves_nothing(self):
        for data, wget, size, seconds in ((b"x" + BLOB[1:], "", None, 20), (BLOB[:-1], "", None, 20),
                                          (BLOB, "", len(BLOB) + 1, 20), (BLOB, "fail", None, 20),
                                          (BLOB, "slow", None, 1)):
            with self.subTest(wget=wget, size=size, same=data == BLOB):
                code, left = self.run_fetch(data, wget, size, seconds)
                self.assertNotEqual(code, 0)
                self.assertEqual(left, [])

    def test_the_password_and_the_command_reach_sudo_whole(self):
        said = []
        controller = types.SimpleNamespace(http_server="http://g", client_password="p'w \"x")
        with mock.patch.object(self.site, "_guest", lambda c, command, seconds: said.append(command) or True):
            self.site._root(controller, 'test "$(stat -c %i a)" = b', 5)
        words = shlex.split(said[0])
        self.assertEqual(words[:3], ["printf", "%s\\n", "p'w \"x"])
        self.assertEqual(words[words.index("|") + 1:], ["sudo", "-S", "sh", "-c", 'test "$(stat -c %i a)" = b'])


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
        elif "wget" in command:
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
        self.fails = False
        case = self

        class DesktopEnv:
            def __init__(self):
                self.setup_controller = Controller(events)

            def _setup_task(self, task_config, use_proxy):
                events.append(("setup", task_config))
                if case.fails:
                    raise RuntimeError("setup")
                return True, True

        self.DesktopEnv = DesktopEnv
        self.site.PATCHES[self.site.DESKTOP_ENV](types.SimpleNamespace(DesktopEnv=DesktopEnv))
        listed = [{"name": n, "revision": 7, "sha3_384": f"{n}-sha", "size": 11}
                  for n in ("x; reboot", "core24", "openboard", "kf6-core24", "mesa-2404", "lxqt-support-core24")]
        self.listed = os.path.join(self.tmp, "snaps.json")
        with open(self.listed, "w") as f:
            json.dump(listed, f)
        self.given = {"OSWORLD2_SNAPS": self.listed, "OSWORLD2_SNAP_URL": "http://10.213.0.1:4242"}

    def run_setup(self, guest):
        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                mock.patch.dict(os.environ, self.given), self.assertLogs("nullius.osworld2", "INFO") as logs:
            out = self.DesktopEnv()._setup_task("task", True)
        return out, [r.getMessage() for r in logs.records]

    def fetched(self):
        return [e for e in self.events if e[0] == "guest" and "wget" in e[1]]

    def test_what_the_guest_lacks_is_fetched_before_the_setup_until_one_fails_then_hit_or_miss(self):
        guest = Guest(self.events, installed={"core24"}, undelivered={"mesa-2404"}, hit={"openboard"})
        out, said = self.run_setup(guest)
        self.assertEqual(out, (True, True))
        self.assertEqual([("/openboard-sha " in e[1], "/kf6-core24-sha " in e[1], "/mesa-2404-sha " in e[1])
                          for e in self.fetched()], [(True, False, False), (False, True, False), (False, False, True)])
        self.assertLess(self.events.index(self.fetched()[-1]), self.events.index(("setup", "task")))
        self.assertTrue(all(e[2] <= self.site.SNAP_SECONDS + 10 for e in self.fetched()))
        self.assertIn("timeout -k 5 ", self.fetched()[0][1])
        self.assertEqual(said, ["snap cache mesa-2404: not delivered", "snap cache openboard: hit",
                                "snap cache kf6-core24: miss"])
        self.assertFalse([e for e in self.events if e[0] == "guest" and "reboot" in e[1]])

    def test_hit_or_miss_has_a_time_of_its_own(self):
        guest = Guest(self.events, hit={"openboard"})
        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                mock.patch.object(self.site, "SNAP_CHECK_SECONDS", 0), \
                self.assertLogs("nullius.osworld2", "INFO") as logs:
            self.site._hits(Controller(self.events), [{"name": "openboard", "revision": 7, "sha3_384": "s"}])
        self.assertEqual(([e for e in self.events if e[0] == "guest"], [r.getMessage() for r in logs.records]),
                         ([], ["snap cache openboard: miss"]))

    def test_with_too_little_time_left_nothing_more_is_fetched(self):
        guest = Guest(self.events)
        with mock.patch.object(self.site, "SNAP_SECONDS", self.site.SNAP_CHECK_SECONDS - 1):
            out, said = self.run_setup(guest)
        self.assertEqual((self.fetched(), said), ([], ["snap cache core24: not delivered"]))

    def test_a_failed_setup_asks_the_guest_nothing_after(self):
        self.fails = True
        guest = Guest(self.events, hit={"openboard"})
        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                mock.patch.dict(os.environ, self.given), self.assertRaises(RuntimeError):
            self.DesktopEnv()._setup_task("task", True)
        self.assertEqual(self.events[-1], ("setup", "task"))

    def test_without_the_list_or_the_address_the_setup_is_the_upstreams_alone(self):
        for drop in ("OSWORLD2_SNAPS", "OSWORLD2_SNAP_URL"):
            with self.subTest(drop):
                self.events.clear()
                guest = Guest(self.events)
                with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=guest.post)}), \
                        mock.patch.dict(os.environ, self.given):
                    del os.environ[drop]
                    self.assertEqual(self.DesktopEnv()._setup_task("task", False), (True, True))
                self.assertEqual(self.events, [("setup", "task")])

    def test_a_guest_that_fails_midway_keeps_what_was_placed_and_the_setup_runs(self):
        guest = Guest(self.events)
        calls = []

        def post(url, json=None, timeout=None):
            calls.append(json["command"])
            if len(calls) > 4:
                raise ConnectionError("guest")
            return guest.post(url, json, timeout)

        with mock.patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=post)}), \
                mock.patch.dict(os.environ, self.given), self.assertLogs("nullius.osworld2", "INFO") as logs:
            self.assertEqual(self.DesktopEnv()._setup_task("task", False), (True, True))
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["snap cache not used: ConnectionError", "snap cache core24: miss",
                          "snap cache openboard: miss"])

    def test_the_setup_is_wrapped_once(self):
        wrapped = self.DesktopEnv._setup_task
        self.site.PATCHES[self.site.DESKTOP_ENV](types.SimpleNamespace(DesktopEnv=self.DesktopEnv))
        self.assertIs(self.DesktopEnv._setup_task, wrapped)


if __name__ == "__main__":
    unittest.main()
