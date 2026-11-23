"""OSWorld2's mail tasks get their profiles signed out of the live mailboxes their assets ship, and started offline."""
from __future__ import annotations

import contextlib
import gzip
import io
import os
import random
import shutil
import tarfile
import tempfile
import unittest
import zlib
from unittest import mock

from tests.test_osworld2_driver import load

PROFILE = ".thunderbird/abc.default-release/"
PREFS = ('user_pref("mail.server.server1.hostname", "mail.example.com");\n'
         'user_pref("mail.server.server1.login_at_startup", true);\n'
         'user_pref("mail.server.server1.check_new_mail", true);\n'
         'user_pref("mail.server.server1.download_on_biff", true);\n'
         '  user_pref("mail.server.server2.check_new_mail",true);\r\n'
         'user_pref("offline.startup_state", 2);\n')
SIGNED_IN = ("logins.json", "logins-backup.json", "signons.sqlite", "cookies.sqlite", "cookies.sqlite-journal", "user.js",
             "storage/default/https+++login.example.com/ls/data.sqlite")


def archive(path, files):
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 1700000000
            t.addfile(info, io.BytesIO(data))


def members(path):
    with tarfile.open(path) as t:
        return {m.name: t.extractfile(m).read() for m in t if m.isfile()}


class SignedOut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        up = os.path.join(self.tmp.name, "up")
        assets = os.path.join(up, "cache", "osworld_v2_assets")
        for d in ("task_006", "task_007", "shared"):
            os.makedirs(os.path.join(assets, d))
        self.files = {PROFILE + "prefs.js": PREFS.encode(), PROFILE + "key4.db": b"k",
                      PROFILE + "Mail/host/Inbox": b"From a\n\nbody\n", PROFILE + "storage/permanent/chrome/idb/x": b"i",
                      **{PROFILE + n: b"fake" for n in SIGNED_IN}}
        archive(os.path.join(assets, "task_006", "thunderbird-profile.tar.gz"), self.files)
        with open(os.path.join(assets, "task_006", "sheet.xlsx"), "wb") as f:
            f.write(b"sheet")
        self.driver = load(up)
        self.unit = os.path.join(self.tmp.name, "unit")
        os.makedirs(self.unit)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, *rel, files=None):
        path = os.path.join(self.driver.ASSETS, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        archive(path, self.files if files is None else files)
        return path

    def tree(self):
        out = {}
        for d, _, files in os.walk(self.tmp.name):
            for f in files:
                with open(os.path.join(d, f), "rb") as h:
                    out[os.path.join(d, f)] = h.read()
        return out

    def test_every_mail_profile_in_any_tasks_assets_is_signed_out_whatever_its_name(self):
        self.add("task_006", "copy.tar.gz")
        self.add("shared", "deep", "profile.tgz")
        self.add("task_010", "kept.tar.gz", files={"data/prefs.js": b'user_pref("browser.x", true);\n', "a": b"b"})
        os.symlink(os.path.join(self.tmp.name, "gone"), os.path.join(self.driver.ASSETS, "shared", "gone"))
        for eid in ("006", "007"):
            root = self.driver._assets(self.unit, eid)
            self.assertEqual(root, os.path.join(self.unit, "assets"))
            for rel in (("task_006", "thunderbird-profile.tar.gz"), ("task_006", "copy.tar.gz"),
                        ("shared", "deep", "profile.tgz")):
                with self.subTest(eid=eid, rel=rel):
                    path = os.path.join(root, *rel)
                    self.assertFalse(os.path.islink(path))
                    got = members(path)
                    self.assertFalse(any(n[len(PROFILE):] in SIGNED_IN for n in got))
                    self.assertNotIn("true", got[PROFILE + "prefs.js"].decode())
            self.assertEqual(os.path.realpath(os.path.join(root, "task_010")),
                             os.path.realpath(os.path.join(self.driver.ASSETS, "task_010")))
            self.assertEqual(os.path.realpath(os.path.join(root, "task_006", "sheet.xlsx")),
                             os.path.realpath(os.path.join(self.driver.ASSETS, "task_006", "sheet.xlsx")))

    def test_an_assets_file_is_read_again_only_once_it_changes(self):
        self.driver._assets(self.unit, "007")
        with mock.patch.object(self.driver, "_mail_profile", wraps=self.driver._mail_profile) as read:
            self.driver._assets(self.unit, "007")
            self.assertEqual(read.call_count, 0)
            path = self.add("task_006", "sheet.xlsx")
            self.driver._assets(self.unit, "007")
        self.assertEqual(read.call_args_list, [mock.call(path)])
        self.assertNotIn("true", members(os.path.join(self.unit, "assets", "task_006", "sheet.xlsx"))[
            PROFILE + "prefs.js"].decode())

    def test_a_mail_profile_in_a_zip_is_refused(self):
        import zipfile
        with zipfile.ZipFile(os.path.join(self.driver.ASSETS, "shared", "p.zip"), "w") as z:
            z.writestr(PROFILE + "prefs.js", PREFS)
        with self.assertRaisesRegex(RuntimeError, "p.zip holds a mail profile in a zip"):
            self.driver._assets(self.unit, "007")

    def test_the_copy_is_the_units_alone_made_anew_each_time_and_goes_with_it(self):
        self.add("shared", "profile.tar.gz")
        with open(os.path.join(self.driver.ASSETS, "shared", "profile.tar.gz.part"), "wb") as f:
            f.write(b"kept")
        os.makedirs(os.path.join(self.unit, "assets"))
        os.symlink(os.path.join(self.driver.ASSETS, "task_006"), os.path.join(self.unit, "assets", "task_006"))
        archive(os.path.join(self.unit, "assets", "left.tar.gz"), self.files)
        before = {p: b for p, b in self.tree().items() if not p.startswith(self.unit + os.sep)}
        for eid in ("006", "007", "006"):
            self.driver._assets(self.unit, eid)
        after = self.tree()
        self.assertEqual({p: b for p, b in after.items() if not p.startswith(self.unit + os.sep)}, before)
        self.assertFalse(os.path.lexists(os.path.join(self.unit, "assets", "left.tar.gz")))
        shutil.rmtree(self.unit)
        self.assertEqual(self.tree(), before)

    def test_a_profile_at_the_archives_root_or_behind_a_zip_is_found_and_signed_out(self):
        import zipfile
        self.add("shared", "root.tar.gz", files={n[len(PROFILE):]: b for n, b in self.files.items()})
        both = self.add("shared", "both.tar.gz")
        with open(both, "ab") as f, zipfile.ZipFile(f, "w") as z:
            z.writestr("note.txt", "x")
        root = self.driver._assets(self.unit, "007")
        got = members(os.path.join(root, "shared", "root.tar.gz"))
        self.assertEqual(sorted(got), sorted(n[len(PROFILE):] for n in self.files if n[len(PROFILE):] not in SIGNED_IN))
        self.assertNotIn("true", got["prefs.js"].decode())
        self.assertTrue(got["prefs.js"].decode().endswith('user_pref("offline.startup_state", 3);\n'))
        got = members(os.path.join(root, "shared", "both.tar.gz"))
        self.assertFalse(any(n[len(PROFILE):] in SIGNED_IN for n in got))

    def test_a_mail_profile_not_in_gzip_is_refused(self):
        with tarfile.open(os.path.join(self.driver.ASSETS, "shared", "plain.tar"), "w") as t:
            info = tarfile.TarInfo(PROFILE + "prefs.js")
            info.size = len(PREFS)
            t.addfile(info, io.BytesIO(PREFS.encode()))
        with self.assertRaisesRegex(RuntimeError, "plain.tar is a mail profile not in gzip"):
            self.driver._assets(self.unit, "007")

    def test_an_archive_read_only_in_part_is_refused_and_read_again_next_time(self):
        cut = os.path.join(self.driver.ASSETS, "shared", "cut.tar")
        with tarfile.open(cut, "w") as t:
            data = PREFS.encode() + b"//" + b"x" * 4000 + b"\n"
            info = tarfile.TarInfo(PROFILE + "prefs.js")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        os.truncate(cut, 1500)
        with open(os.path.join(self.driver.ASSETS, "shared", "log.txt.gz"), "wb") as f:
            f.write(gzip.compress(random.Random(1).randbytes(4000))[:300])
        with self.assertRaisesRegex(RuntimeError, "cut.tar in the assets cannot be read through"):
            self.driver._assets(self.unit, "007")
        os.remove(cut)
        if os.geteuid() == 0:
            return
        shut = self.add("shared", "shut.tar.gz")
        os.chmod(shut, 0)
        with self.assertRaisesRegex(RuntimeError, "shut.tar.gz in the assets cannot be read through"):
            self.driver._assets(self.unit, "007")
        os.chmod(shut, 0o644)
        root = self.driver._assets(self.unit, "007")
        self.assertNotIn("true", members(os.path.join(root, "shared", "shut.tar.gz"))[PROFILE + "prefs.js"].decode())
        for mode, rel in ((0, "locked"), (0o444, "dim")):
            folder = os.path.dirname(self.add("shared", rel, "p.tar.gz"))
            os.chmod(folder, mode)
            with self.subTest(rel), self.assertRaisesRegex(RuntimeError, "cannot be read to look for mail profiles"):
                self.driver._assets(self.unit, "007")
            os.chmod(folder, 0o755)

    def test_the_profile_loses_its_sign_ins_and_keeps_its_mail(self):
        root = self.driver._assets(self.unit, "006")
        got = members(os.path.join(root, "task_006", "thunderbird-profile.tar.gz"))
        self.assertEqual(sorted(got), sorted(n for n in self.files if n[len(PROFILE):] not in SIGNED_IN))
        self.assertEqual(got[PROFILE + "Mail/host/Inbox"], self.files[PROFILE + "Mail/host/Inbox"])
        prefs = got[PROFILE + "prefs.js"].decode()
        self.assertNotIn("true", prefs)
        self.assertIn('"mail.server.server1.hostname", "mail.example.com"', prefs)
        self.assertTrue(prefs.endswith('user_pref("offline.autoDetect", false);\n'
                                       'user_pref("offline.startup_state", 3);\n'))

    def test_the_rest_of_the_assets_are_the_originals(self):
        root = self.driver._assets(self.unit, "006")
        for rel in ("shared", "task_007", os.path.join("task_006", "sheet.xlsx")):
            self.assertEqual(os.path.realpath(os.path.join(root, rel)),
                             os.path.realpath(os.path.join(self.driver.ASSETS, rel)))
        self.assertEqual([n for n in os.listdir(os.path.join(root, "task_006")) if n.endswith(".part")], [])

    def test_the_copy_is_the_same_each_time(self):
        root = self.driver._assets(self.unit, "006")
        with open(os.path.join(root, "task_006", "thunderbird-profile.tar.gz"), "rb") as f:
            first = f.read()
        self.driver._assets(self.unit, "006")
        with open(os.path.join(root, "task_006", "thunderbird-profile.tar.gz"), "rb") as f:
            self.assertEqual(f.read(), first)

    def test_a_missing_profile_is_refused(self):
        os.remove(os.path.join(self.driver.ASSETS, "task_006", "thunderbird-profile.tar.gz"))
        with self.assertRaisesRegex(RuntimeError, "no thunderbird-profile.tar.gz"):
            self.driver._assets(self.unit, "006")

    def test_a_hard_link_is_refused(self):
        path = os.path.join(self.driver.ASSETS, "task_006", "thunderbird-profile.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            link = tarfile.TarInfo(PROFILE + "prefs.js")
            link.type, link.linkname = tarfile.LNKTYPE, PROFILE + "key4.db"
            t.addfile(link)
        with self.assertRaisesRegex(RuntimeError, "does not rewrite"):
            self.driver._assets(self.unit, "006")
        self.assertEqual([n for n in os.listdir(os.path.join(self.unit, "assets", "task_006")) if n.endswith(".part")], [])

    def test_a_broken_profile_archive_is_a_setup_error(self):
        path = os.path.join(self.driver.ASSETS, "task_006", "thunderbird-profile.tar.gz")
        with open(path, "rb") as f:
            whole = f.read()
        patches = {"_vm": lambda *a: "vm", "_task": lambda *_: {"instruction": "", "proxy": False}}
        for name, data, fault in (("not an archive", b"x" * 600, None), ("cut short", whole[:len(whole) // 2], None),
                                  ("bad deflate data", whole, zlib.error("invalid stored block lengths"))):
            with open(path, "wb") as f:
                f.write(data)
            with self.subTest(name), mock.patch.multiple(self.driver, **patches), \
                    mock.patch.object(self.driver, "_signed_out", side_effect=fault) if fault else contextlib.nullcontext(), \
                    self.assertRaises(self.driver.EnvironmentSetupError):
                self.driver.score("tasks__006", self.unit)

    def test_the_null_control_and_the_original_get_the_copy(self):
        envs = []

        def run(argv, cwd, env, *a, **kw):
            envs.append(env)
            raise KeyboardInterrupt

        with mock.patch.object(self.driver, "_vm", lambda *a: "vm"), mock.patch.object(self.driver, "_run", run), \
                mock.patch.object(self.driver, "_task", lambda *_: {"instruction": "", "proxy": False}):
            for start in (lambda: self.driver.score("tasks__006", self.unit),
                          lambda: self.driver.answer("tasks__006", self.unit, "original", "MiniMax-M3",
                                                     "http://127.0.0.1:9/v1", "k"),
                          lambda: self.driver.score("tasks__012", self.unit)):
                with self.assertRaises(KeyboardInterrupt):
                    start()
        self.assertEqual([e["OSWORLD_FILE_BASE_URL"] for e in envs], [os.path.join(self.unit, "assets")] * 3)
        self.assertEqual(self.driver._env("007")["OSWORLD_FILE_BASE_URL"], self.driver.ASSETS)

    def test_the_agents_desktop_gets_the_copy(self):
        self.add("shared", "profile.tar.gz")
        made = []

        def env(eid, assets=None, **extra):
            made.append(assets)
            raise KeyboardInterrupt

        with mock.patch.object(self.driver, "_vm", lambda *a: "vm"), mock.patch.object(self.driver, "_env", env), \
                mock.patch.object(self.driver, "_task", lambda *_: {"instruction": "", "proxy": False}), \
                self.assertRaises(KeyboardInterrupt):
            self.driver.setup("tasks__012", self.unit, participant="agent")
        self.assertEqual(made, [os.path.join(self.driver._runner_dir(self.unit), "assets")])
        got = members(os.path.join(made[0], "shared", "profile.tar.gz"))
        self.assertFalse(any(n[len(PROFILE):] in SIGNED_IN for n in got))
        self.assertNotIn("true", got[PROFILE + "prefs.js"].decode())


if __name__ == "__main__":
    unittest.main()
