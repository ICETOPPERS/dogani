"""설정 옮기기·보관·업데이트·exe 의존성 시험 (dogani.py 를 import 하므로 PySide6 가 있는 파이썬으로)

설정·보관 파일 경로는 시험마다 임시 폴더로 바꿔 끼워 실제 %APPDATA%\\dogani 를 건드리지 않음
"""
import ast
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
_HOME = tempfile.mkdtemp(prefix="dogani-home-")
os.environ.setdefault("DOGANI_HOME", _HOME)   # import 때 정해지는 경로가 실제 설정을 가리키지 않게
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, APP)

import codesign  # noqa: E402
import dogani  # noqa: E402
import space  # noqa: E402
import updater  # noqa: E402

TEST_KEY = bytes(range(32))   # 시험용 배포 서명 키 (updater.SIGN_KEYS 를 이 공개키로 바꿔 씀)
TEST_PUB = codesign.ed25519.public_key(TEST_KEY).hex()


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        space.BROKEN.clear()
        for k, v in {"CONFIG_DIR": self.tmp, "CONFIG_PATH": os.path.join(self.tmp, "dogani.json"),
                     "UNSAVED_PATH": os.path.join(self.tmp, "unsaved.json")}.items():
            p = mock.patch.object(dogani, k, v)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        space.BROKEN.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, data):
        with open(dogani.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))


class LoadConfigTest(TempHome):
    def test_missing_and_broken(self):
        cfg = dogani.load_config()
        self.assertEqual((cfg["port"], cfg["spaces"], cfg["theme"]), (dogani.DEFAULT_PORT, [], "light"))
        self.assertTrue(cfg["name"])                      # 처음엔 PC 이름
        self.put("{broken")
        self.assertEqual(dogani.load_config()["spaces"], [])
        self.assertEqual(len(space.BROKEN), 1)            # 덮어쓰기 전에 백업
        self.assertTrue(any(n.startswith("dogani.json.broken-") for n in os.listdir(self.tmp)))

    def test_old_keys_and_shapes(self):
        """예전 nav 창·전체 숨김(visible) 정리, 모양이 틀린 값·스페이스는 버림"""
        self.put({"name": "나", "visible": False, "cal_hidden": "x", "windows": {"nav": {}, "note": {"open": True}},
                  "spaces": [{"kind": "local", "dir": "C:/a"}, {"kind": "local"}, {"kind": "join", "host": "h"},
                             {"kind": "join", "host": "h", "token": "t"}, {"kind": "other", "dir": "C:/b"}, "x"]})
        cfg = dogani.load_config()
        self.assertEqual(cfg["windows"], {"note": {"open": False}})
        self.assertEqual(cfg["cal_hidden"], [])
        self.assertEqual([s.get("dir") or s.get("token") for s in cfg["spaces"]], ["C:/a", "t"])
        self.assertEqual(cfg["name"], "나")
        self.put({"spaces": "x"})
        self.assertEqual(dogani.load_config()["spaces"], [])

    def test_save_roundtrip(self):
        cfg = dogani.load_config()
        cfg["name"] = "한글 이름"
        dogani.save_config(cfg)
        self.assertEqual(dogani.load_config()["name"], "한글 이름")
        self.assertFalse(os.path.exists(dogani.CONFIG_PATH + ".tmp"))


class MigrateTest(unittest.TestCase):
    """v26.11.18 이전 설정(개인 하나 + 팀 하나) → spaces 목록. 예전 키는 남김(예전 exe 와 번갈아 켜도 되게)"""

    def mig(self, cfg):
        cfg = dict(cfg)
        dogani.migrate_spaces(cfg)
        return cfg

    def test_personal_and_team_host(self):
        cfg = self.mig({"space_dir": "C:/me", "space_names": {"C:/me": "내 노트"}, "team_host": True,
                        "team_space_dir": "C:/team", "team": {"team": "전산팀"}})
        self.assertEqual(cfg["spaces"], [
            {"kind": "local", "dir": "C:/me", "name": "내 노트", "legacy": dogani.ME},
            {"kind": "local", "dir": "C:/team", "name": "전산팀", "legacy": dogani.TEAM}])
        self.assertEqual(cfg["space_dir"], "C:/me")

    def test_team_host_defaults(self):
        cfg = self.mig({"space_dir": "C:/me", "team_host": True, "team_space_dir": "C:/me", "space_names": "x"})
        self.assertEqual([(s["dir"], s["name"]) for s in cfg["spaces"]], [("C:/me", "me")])   # 같은 폴더는 하나
        cfg = self.mig({"team_host": True})
        self.assertEqual(cfg["spaces"][0]["dir"], dogani.LEGACY_TEAM_SPACE)

    def test_joined_team(self):
        t = {"token": "tok", "host": "10.0.0.5:8765", "pc": "PC1", "id": "1a2b3c4d", "pub": "ab", "team": "전산팀"}
        self.assertEqual(self.mig({"team": t})["spaces"], [
            {"kind": "join", "id": "", "host": "10.0.0.5:8765", "pc": "PC1", "token": "tok", "mid": "1a2b3c4d",
             "pub": "ab", "name": "전산팀", "legacy": dogani.TEAM}])
        self.assertEqual(self.mig({"team": dict(t, host="127.0.0.1:8765")})["spaces"], [])   # 제 PC 참여는 버림
        self.assertEqual(self.mig({"team": "x"})["spaces"], [])

    def test_already_migrated(self):
        cfg = {"space_dir": "C:/me", "spaces": [{"kind": "local", "dir": "C:/new"}]}
        self.assertEqual(self.mig(cfg)["spaces"], [{"kind": "local", "dir": "C:/new"}])
        self.assertNotIn("spaces", self.mig({"port": 1}))   # 예전 키가 없으면 손대지 않음


class UnsavedTest(TempHome):
    def test_save_and_drop(self):
        dogani.save_unsaved("a|2026-11-20", json.dumps({"at": 1, "text": "가"}))
        dogani.save_unsaved("bad", "{not json")
        self.assertEqual(dogani.load_unsaved(), {"a|2026-11-20": {"at": 1, "text": "가"}})
        dogani.save_unsaved("a|2026-11-20", "")
        self.assertEqual(dogani.load_unsaved(), {})
        dogani.save_unsaved("none", "")          # 없는 키 지우기 = 아무 일 없음

    def test_keep_newest_50(self):
        for i in range(55):
            dogani.save_unsaved(f"k{i}", json.dumps({"at": 1000 + i}))
        dogani.save_unsaved("old-shape", json.dumps("글자만"))   # dict 가 아니면 가장 오래된 것으로 침
        d = dogani.load_unsaved()
        self.assertEqual(len(d), 50)
        self.assertNotIn("old-shape", d)
        self.assertEqual(min(v["at"] for v in d.values()), 1005)   # 1000~1004 는 이미 밀려남

    def test_broken_file(self):
        with open(dogani.UNSAVED_PATH, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        self.assertEqual(dogani.load_unsaved(), {})
        dogani.save_unsaved("k", json.dumps({"at": 1}))
        self.assertEqual(dogani.load_unsaved(), {"k": {"at": 1}})


def make_zip(files, top="dogani-v26.11.21.01"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(f"{top}/{name}", data)
    return buf.getvalue()


class StageTest(unittest.TestCase):
    """updater.stage: 최신 태그 zip 의 app/ 만 받아 STAGED_DIR 에, version.json 은 마지막에"""
    TAG = "v26.11.21.01"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        self.staged = os.path.join(self.tmp, "app.new")
        app = {"dogani.py": b"print(1)", "ui/app.js": b"//", "editor/big.js": b"//"}
        self.zip = make_zip({"README.md": "x", **{"app/" + k: v for k, v in app.items()},
                             "app/release.sig": codesign.make(self.TAG, app, TEST_KEY)})
        self.patch("SIGN_KEYS", (TEST_PUB,))
        self.patch("STAGED_DIR", self.staged)
        self.patch("REJECTED_FILE", os.path.join(self.tmp, "app.rejected"))   # 거절 기억이 다른 시험으로 새지 않게
        self.patch("latest_tag", lambda timeout=5: self.TAG)
        self.patch("current_version", lambda: {"id": "v26.11.20.01"})
        self.patch("_get", lambda url, timeout: self.zip)

    def patch(self, name, fn):
        p = mock.patch.object(updater, name, fn)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def files(self):
        out = []
        for root, _, names in os.walk(self.staged):
            out += [os.path.relpath(os.path.join(root, n), self.staged).replace("\\", "/") for n in names]
        return sorted(out)

    def test_newer_tag(self):
        self.assertTrue(updater.stage())
        self.assertEqual(self.files(), ["dogani.py", "release.sig", "ui/app.js", "version.json"])   # editor/ 는 안 받음
        with open(os.path.join(self.staged, "version.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"id": self.TAG, "label": self.TAG})

    def test_not_newer_or_no_tag(self):
        self.patch("current_version", lambda: {"id": self.TAG})
        self.assertFalse(updater.stage())
        self.patch("latest_tag", lambda timeout=5: None)
        self.assertFalse(updater.stage())
        self.assertFalse(os.path.exists(self.staged))

    def test_bad_zips(self):
        """dogani.py 없는 zip·.. 경로·깨진 zip·받기 실패는 False 이고 STAGED_DIR 을 만들지 않음"""
        for data in (make_zip({"app/other.py": "x"}), make_zip({"app/dogani.py": "x", "app/../evil.py": "x"}),
                     b"not a zip", make_zip({})):
            self.zip = data
            self.assertFalse(updater.stage())
            self.assertFalse(os.path.exists(self.staged))
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.py")))

        def fail(url, timeout):
            raise OSError("offline")
        self.patch("_get", fail)
        self.assertFalse(updater.stage())

class LatestTagTest(unittest.TestCase):
    def test_tag_order(self):
        """latest_tag: 버전 모양 태그만, 문자열 최댓값 = 최신, 못 읽으면 None"""
        tags = [{"name": "v26.11.20.01"}, {"name": "v26.11.21.02"}, {"name": "v26.11.3.01"}, {"name": "test"}, {}]
        with mock.patch.object(updater, "_get", lambda url, timeout: json.dumps(tags).encode()):
            self.assertEqual(updater.latest_tag(), "v26.11.21.02")
        with mock.patch.object(updater, "_get", lambda url, timeout: b"{"):
            self.assertIsNone(updater.latest_tag())


class LauncherUpdateTest(unittest.TestCase):
    """런처(exe) 업데이트: 최신 Release 설치 파일을 받아 두고(stage_launcher) 다음에 켤 때 조용히 설치(apply_launcher).
    동료 PC 에서 내려받은 exe 를 실행하는 경로라 '받은 그대로인 새 버전만, 한 태그는 한 번만' 을 본다"""
    OLD, NEW = "v26.11.19.02", "v26.11.24.01"
    DATA = b"setup-bytes" * 100
    REAL_RELEASE = staticmethod(updater.latest_release)  # setUp 이 바꿔 끼우기 전 것

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        setup = os.path.join(self.tmp, "setup")
        meipass = os.path.join(self.tmp, "meipass")
        os.makedirs(os.path.join(meipass, "app"))
        with open(os.path.join(meipass, "app", "version.json"), "w", encoding="utf-8") as f:
            json.dump({"id": self.OLD, "label": self.OLD}, f)
        for name, value in {"SETUP_DIR": setup, "SETUP_EXE": os.path.join(setup, "dogani-setup.exe"),
                            "SETUP_INFO": os.path.join(setup, "setup.json"),
                            "SETUP_DONE": os.path.join(self.tmp, "setup.done"),
                            "SETUP_LOG": os.path.join(self.tmp, "setup.log"), "FROZEN": True,
                            "installed_here": lambda: True, "autostart_on": lambda: True}.items():
            self.patch(updater, name, value)
        self.patch(sys, "_MEIPASS", meipass, create=True)
        self.popen = mock.MagicMock()
        self.patch(updater.subprocess, "Popen", self.popen)
        self.release(self.NEW, self.DATA)

    def patch(self, obj, name, value, **kw):
        p = mock.patch.object(obj, name, value, **kw)
        p.start()
        self.addCleanup(p.stop)

    def release(self, tag, data, size=None, digest="ok"):
        import hashlib
        sha = hashlib.sha256(data).hexdigest() if digest == "ok" else digest
        asset = {"url": f"https://github.com/{updater.REPO}/releases/download/{tag}/dogani-setup.exe",
                 "size": len(data) if size is None else size, "sha256": sha}
        self.patch(updater, "latest_release", lambda timeout=5: (tag, asset))
        self.patch(updater.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(data))

    def staged(self):
        return updater._read_json(updater.SETUP_INFO).get("tag")

    def test_latest_release_shape(self):
        """우리 저장소 Release 주소·버전 모양 태그·크기가 있어야 함. digest 없으면 해시 확인만 건너뜀"""
        url = f"https://github.com/{updater.REPO}/releases/download/{self.NEW}/dogani-setup.exe"
        good = {"tag_name": self.NEW, "assets": [{"name": "other.zip"}, {
            "name": "dogani-setup.exe", "browser_download_url": url, "size": 10, "digest": "sha256:ABC"}]}

        def got(rel):
            with mock.patch.object(updater, "_get", lambda u, timeout: json.dumps(rel).encode()):
                return self.REAL_RELEASE()
        self.assertEqual(got(good), (self.NEW, {"url": url, "size": 10, "sha256": "abc"}))
        no_digest = json.loads(json.dumps(good))
        del no_digest["assets"][1]["digest"]
        self.assertEqual(got(no_digest)[1]["sha256"], "")
        for bad in ({**good, "tag_name": "test"}, {**good, "assets": [good["assets"][0]]}, [],
                    {**good, "assets": [{**good["assets"][1], "browser_download_url": "https://evil/x.exe"}]},
                    {**good, "assets": [{**good["assets"][1], "size": "10"}]}):
            self.assertIsNone(got(bad))

    def test_stage_newer(self):
        self.assertTrue(updater.stage_launcher())
        with open(updater.SETUP_EXE, "rb") as f:
            self.assertEqual(f.read(), self.DATA)
        self.assertEqual(self.staged(), self.NEW)
        self.assertFalse(updater.stage_launcher())  # 이미 받아 둠

    def test_stage_skips(self):
        """같은 버전·소스 실행·폴더째 복사한 exe 는 받지 않음"""
        self.release(self.OLD, self.DATA)
        self.assertFalse(updater.stage_launcher())
        self.release(self.NEW, self.DATA)
        for name, value in (("FROZEN", False), ("installed_here", lambda: False)):
            with mock.patch.object(updater, name, value):
                self.assertFalse(updater.stage_launcher())
        self.assertFalse(os.path.exists(updater.SETUP_EXE))

    def test_stage_rejects_wrong_size_or_hash(self):
        for kw in ({"size": len(self.DATA) + 1}, {"digest": "0" * 64}):
            self.release(self.NEW, self.DATA, **kw)
            self.assertFalse(updater.stage_launcher())
            self.assertIsNone(self.staged())
            self.assertEqual([n for n in os.listdir(updater.SETUP_DIR)], [])  # .tmp 도 안 남음

    def test_apply_runs_once_then_cleans(self):
        updater.stage_launcher()
        self.assertTrue(updater.apply_launcher())
        cmd = self.popen.call_args[0][0]
        self.assertEqual(cmd[0], updater.SETUP_EXE)
        self.assertIn("/VERYSILENT", cmd)
        self.assertIn("/MERGETASKS=autostart", cmd)
        # 설치가 실패해 예전 exe 그대로여도 같은 태그는 다시 안 돌리고 치움
        self.assertFalse(updater.apply_launcher())
        self.assertEqual(self.popen.call_count, 1)
        self.assertFalse(os.path.exists(updater.SETUP_DIR))

    def test_apply_skips(self):
        """바뀐 설치 파일은 지움, 폴더째 복사한 exe·소스 실행은 그대로 둠, 이미 새 exe 면 치움"""
        updater.stage_launcher()
        with mock.patch.object(updater, "FROZEN", False):
            self.assertFalse(updater.apply_launcher())
        with mock.patch.object(updater, "installed_here", lambda: False):
            self.assertFalse(updater.apply_launcher())
        self.assertEqual(self.staged(), self.NEW)
        with open(updater.SETUP_EXE, "ab") as f:
            f.write(b"x")
        self.assertFalse(updater.apply_launcher())
        self.assertFalse(os.path.exists(updater.SETUP_DIR))

        updater.stage_launcher()
        with open(os.path.join(sys._MEIPASS, "app", "version.json"), "w", encoding="utf-8") as f:
            json.dump({"id": self.NEW}, f)  # 그사이 새 설치 파일을 직접 깔았음
        self.assertFalse(updater.apply_launcher())
        self.assertFalse(os.path.exists(updater.SETUP_DIR))
        self.popen.assert_not_called()

    def test_popen_failure_not_counted(self):
        """설치 파일을 못 띄웠으면(백신 등) 시도로 치지 않음 → 다음에 켤 때 다시"""
        updater.stage_launcher()
        self.popen.side_effect = OSError("blocked")
        self.assertFalse(updater.apply_launcher())
        self.assertFalse(os.path.exists(updater.SETUP_DONE))
        self.assertEqual(self.staged(), self.NEW)

    def test_stage_cleans_leftover(self):
        """설치 직후 못 지운 설치 파일·중간에 끊긴 .tmp 는 다음 확인 때 치움"""
        os.makedirs(updater.SETUP_DIR)
        for name in ("dogani-setup.exe", "dogani-setup.exe.tmp"):
            with open(os.path.join(updater.SETUP_DIR, name), "wb") as f:
                f.write(b"old")
        self.release(self.OLD, self.DATA)
        self.assertFalse(updater.stage_launcher())
        self.assertFalse(os.path.exists(updater.SETUP_DIR))

    def test_assets_null(self):
        with mock.patch.object(updater, "_get", lambda u, timeout: b'{"tag_name": "v26.11.24.01", "assets": null}'):
            self.assertIsNone(self.REAL_RELEASE())

    def test_autostart_off_kept(self):
        with mock.patch.object(updater, "autostart_on", lambda: False):
            cmd = updater.setup_command()
        self.assertIn("/MERGETASKS=!autostart", cmd)
        self.assertIn("/RELAUNCH=1", cmd)  # 설치가 끝나면 installer.iss 가 다시 띄움

    def test_stage_exits_for_install(self):
        """켤 때 stage() 가 설치를 시작하면 dogani.exe 를 놓도록 바로 끝남 (코드 zip 은 안 받음)"""
        with mock.patch.object(updater, "apply_launcher", lambda: True), \
                mock.patch.object(updater.os, "_exit", side_effect=SystemExit) as ex, \
                mock.patch.object(updater, "latest_tag") as tag:
            with self.assertRaises(SystemExit):
                updater.stage()
            ex.assert_called_once_with(0)
            tag.assert_not_called()


class DepsTest(unittest.TestCase):
    """받아 오는 코드(app/*.py)가 import 하는 모듈은 deps.py 를 실행했을 때 모두 올라와 있어야 함
    (exe 는 PyInstaller 가 deps.py 에서 찾은 것만 담음. 빠지면 업데이트 뒤 동료 PC 에서 import 실패)"""

    def test_imports_in_deps(self):
        local = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(APP, "*.py"))}
        need = {}
        for p in glob.glob(os.path.join(APP, "*.py")):
            name = os.path.basename(p)
            if name in ("launcher.py", "deps.py"):   # 런처는 exe 자체라 그 import 는 늘 들어감
                continue
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for m in mods:
                    if m.split(".")[0] not in local:
                        need.setdefault(m, set()).add(name)
        code = ("import json, runpy, sys; runpy.run_path(sys.argv[1]); "
                "print(json.dumps(sorted(sys.modules)))")
        out = subprocess.run([sys.executable, "-c", code, os.path.join(APP, "deps.py")], capture_output=True,
                             cwd=tempfile.gettempdir(), timeout=120,
                             env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        self.assertEqual(out.returncode, 0, out.stderr.decode(errors="replace"))
        loaded = set(json.loads(out.stdout))
        missing = {m: sorted(v) for m, v in need.items() if m not in loaded}
        self.assertEqual(missing, {}, "deps.py 에 추가하고 exe 를 다시 빌드해야 함")


if __name__ == "__main__":
    unittest.main(verbosity=2)
