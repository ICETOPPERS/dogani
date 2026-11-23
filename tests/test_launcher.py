"""런처(launcher.py) 시험: 받은 코드가 깨졌을 때 내장 코드로 넘어가기, 죽은 버전 기억, 로그

동료 PC 에서 받은 코드가 import·시작 중에 죽어도 dogani 가 켜져야 하므로 그 경로만 본다.
코드 폴더는 시험마다 임시 폴더에 가짜 dogani.py 로 만들고, sys.modules·sys.path·환경 변수는 끝나면 되돌림.
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
os.environ.setdefault("DOGANI_HOME", tempfile.mkdtemp(prefix="dogani-home-"))
sys.path.insert(0, APP)
# deps 는 exe 에 의존성을 넣으려고 PySide6 등을 몽땅 import 함 → 런처 동작 시험엔 필요 없음
sys.modules.setdefault("deps", types.ModuleType("deps"))

import launcher  # noqa: E402

REAL_FETCH = launcher.fetch_update

OLD, NEW = "v26.11.20.01", "v26.11.23.01"


def make_code(folder, vid, dogani_src, extra=None):
    """가짜 코드 폴더: dogani.py + version.json (+ 다른 모듈)"""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "dogani.py"), "w", encoding="utf-8") as f:
        f.write(dogani_src)
    for name, src in (extra or {}).items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(src)
    with open(os.path.join(folder, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"id": vid, "label": vid}, f)


# main() 이 불리면 어느 코드가 돌았는지 launcher 모듈에 적어 둠
RAN = "import launcher\ndef main():\n    launcher.RAN = {which!r}\n"


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-launcher-")
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        paths = {"CONFIG_DIR": home, "CACHE_DIR": os.path.join(home, "app"),
                 "STAGED_DIR": os.path.join(home, "app.new"), "BAD_FILE": os.path.join(home, "app.bad"),
                 "LOG_FILE": os.path.join(home, "launcher.log"), "BUNDLED_DIR": os.path.join(self.tmp, "bundled")}
        for k, v in paths.items():
            p = mock.patch.object(launcher, k, v)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)),
                  mock.patch.dict(os.environ), mock.patch.object(launcher, "already_running", lambda: False),
                  mock.patch.object(launcher, "fetch_update", lambda: None)):
            p.start()
            self.addCleanup(p.stop)
        sys.modules.pop("dogani", None)   # 다른 시험이 올린 진짜 dogani 대신 가짜를 import 하게
        launcher.RAN = None
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        make_code(launcher.BUNDLED_DIR, OLD, RAN.format(which="bundled"))

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_new_cache_runs(self):
        make_code(launcher.CACHE_DIR, NEW, RAN.format(which="cache"))
        launcher.main()
        self.assertEqual(launcher.RAN, "cache")

    def test_older_cache_uses_bundled(self):
        make_code(launcher.CACHE_DIR, "v26.11.01.01", RAN.format(which="cache"))
        launcher.main()
        self.assertEqual(launcher.RAN, "bundled")

    def test_import_failure_falls_back_and_drops_modules(self):
        # 받은 코드가 다른 모듈을 반쯤 올린 뒤 죽음 → 그 모듈도 내려야 내장 코드가 자기 것을 씀
        make_code(launcher.CACHE_DIR, NEW, "import helper_mod\nraise ImportError('깨짐')\n",
                  {"helper_mod.py": "WHO = 'cache'\n"})
        make_code(launcher.BUNDLED_DIR, OLD, "import launcher, helper_mod\n"
                  "def main():\n    launcher.RAN = 'bundled-' + helper_mod.WHO\n",
                  {"helper_mod.py": "WHO = 'bundled'\n"})
        launcher.main()
        self.assertEqual(launcher.RAN, "bundled-bundled")
        self.assertNotIn(launcher.CACHE_DIR, sys.path)
        self.assertEqual(launcher.bad_version(), NEW)
        log = self.read(launcher.LOG_FILE)
        self.assertIn("import 실패", log)
        self.assertIn("ImportError", log)   # traceback 도 남김

    def test_entry_failure_restarts_bundled(self):
        make_code(launcher.CACHE_DIR, NEW, "def main():\n    raise RuntimeError('시작 실패')\n")
        with mock.patch.object(launcher, "restart_bundled") as restart:
            launcher.main()
        restart.assert_called_once()
        self.assertIsNone(launcher.RAN)
        self.assertEqual(launcher.bad_version(), NEW)
        self.assertIn("RuntimeError", self.read(launcher.LOG_FILE))
        # 폴더는 남기되 다음 실행부터 건너뜀
        self.assertTrue(os.path.isdir(launcher.CACHE_DIR))
        self.assertEqual(launcher.code_dir(), launcher.BUNDLED_DIR)

    def test_unknown_version_is_deleted(self):
        # 버전을 모르면 app.bad 로 기억할 수 없으니 지워야 다음에 또 안 잡힘
        make_code(launcher.CACHE_DIR, NEW, "def main():\n    raise RuntimeError('x')\n")
        os.remove(os.path.join(launcher.CACHE_DIR, "version.json"))
        launcher.mark_bad(launcher.CACHE_DIR)
        self.assertFalse(os.path.exists(launcher.CACHE_DIR))
        self.assertEqual(launcher.bad_version(), "")

    def test_safe_bundled_failure_clears_bad(self):
        # 받은 코드·내장 코드 둘 다 시작하다 죽음 = 환경 탓 → 받은 코드 표시를 풀어 다음엔 다시 받은 코드로
        make_code(launcher.CACHE_DIR, NEW, RAN.format(which="cache"))
        make_code(launcher.BUNDLED_DIR, OLD, "def main():\n    raise OSError('포트')\n")
        launcher.write_bad(NEW)
        os.environ[launcher.SAFE_ENV] = "1"
        with mock.patch.object(launcher, "wait_parent"), self.assertRaises(OSError):
            launcher.main()
        self.assertEqual(launcher.bad_version(), "")
        self.assertIn("내장 코드도 실행 실패", self.read(launcher.LOG_FILE))
        self.assertEqual(launcher.code_dir(), launcher.CACHE_DIR)

    def test_bundled_failure_logged(self):
        make_code(launcher.BUNDLED_DIR, OLD, "def main():\n    raise ValueError('내장')\n")
        with self.assertRaises(ValueError):
            launcher.main()
        self.assertIn("ValueError", self.read(launcher.LOG_FILE))

    def test_fetch_update_skips_bad_tag(self):
        # 죽은 태그를 이미 가진 것처럼 → stage() 가 같은 zip 을 켤 때마다 또 받지 않음
        with open(os.path.join(launcher.BUNDLED_DIR, "updater.py"), "w", encoding="utf-8") as f:
            f.write("import launcher\n"
                    "def current_version():\n    return {'id': %r}\n"
                    "def stage():\n    launcher.SEEN = current_version()['id']\n" % OLD)
        launcher.write_bad(NEW)
        REAL_FETCH()   # setUp 은 main() 시험용으로 fetch_update 를 막아 둠
        self.assertEqual(launcher.SEEN, NEW)

    def test_system_exit_is_normal(self):
        make_code(launcher.CACHE_DIR, NEW, "import sys\ndef main():\n    sys.exit(0)\n")
        with mock.patch.object(launcher, "restart_bundled") as restart, self.assertRaises(SystemExit):
            launcher.main()
        restart.assert_not_called()
        self.assertTrue(os.path.isdir(launcher.CACHE_DIR))

    def test_safe_mode_uses_bundled_and_clears_flag(self):
        # 다시 띄운 쪽: 받은 코드가 덜 지워져 남아 있어도 내장 코드만, 표시는 지워 다음 재시작엔 안 물려줌
        make_code(launcher.CACHE_DIR, NEW, RAN.format(which="cache"))
        os.environ[launcher.SAFE_ENV] = "1"
        with mock.patch.object(launcher, "wait_parent") as wait, \
                mock.patch.object(launcher, "apply_staged") as apply:
            launcher.main()
        wait.assert_called_once()
        apply.assert_not_called()
        self.assertEqual(launcher.RAN, "bundled")
        self.assertNotIn(launcher.SAFE_ENV, os.environ)

    def test_restart_bundled_sets_flag_and_exits(self):
        with mock.patch.object(launcher.subprocess, "Popen") as popen, \
                mock.patch.object(launcher.os, "_exit") as exit_:
            launcher.restart_bundled()
        self.assertEqual(popen.call_args.kwargs["env"][launcher.SAFE_ENV], "1")
        exit_.assert_called_once()

    def test_apply_staged_skips_bad_version(self):
        make_code(launcher.CACHE_DIR, OLD, RAN.format(which="cache"))
        make_code(launcher.STAGED_DIR, NEW, RAN.format(which="staged"))
        with open(launcher.BAD_FILE, "w", encoding="utf-8") as f:
            f.write(NEW)
        launcher.apply_staged()
        self.assertFalse(os.path.exists(launcher.STAGED_DIR))
        self.assertEqual(launcher.version_id(launcher.CACHE_DIR), OLD)

    def test_apply_staged_newer_than_bad(self):
        make_code(launcher.STAGED_DIR, "v26.11.24.01", RAN.format(which="staged"))
        with open(launcher.BAD_FILE, "w", encoding="utf-8") as f:
            f.write(NEW)
        launcher.apply_staged()
        self.assertEqual(launcher.version_id(launcher.CACHE_DIR), "v26.11.24.01")

    def test_apply_staged_incomplete(self):
        os.makedirs(launcher.STAGED_DIR)   # version.json 없음 = 받다 끊김
        launcher.apply_staged()
        self.assertTrue(os.path.isdir(launcher.STAGED_DIR))
        self.assertFalse(os.path.exists(launcher.CACHE_DIR))

    def test_broken_version_json(self):
        os.makedirs(launcher.CACHE_DIR)
        for body in ("{", "[]", '{"id": 5}', '{"id": null}'):   # 숫자·null 은 버전 비교에서 TypeError
            with open(os.path.join(launcher.CACHE_DIR, "version.json"), "w", encoding="utf-8") as f:
                f.write(body)
            self.assertEqual(launcher.version_id(launcher.CACHE_DIR), "")

    def test_log_truncates_when_big(self):
        with open(launcher.LOG_FILE, "w", encoding="utf-8") as f:
            f.write("x" * (launcher.LOG_MAX + 1))
        launcher.log("새 줄")
        text = self.read(launcher.LOG_FILE)
        self.assertNotIn("xxx", text)
        self.assertIn("새 줄", text)


if __name__ == "__main__":
    unittest.main()
