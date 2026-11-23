"""배포 서명 시험 (codesign·updater.stage·tools/sign_release.py).
동료 PC 에서 받은 코드가 실행되는 경로라, 서명이 조금이라도 안 맞으면 받지 않는지와
실제 git 저장소 → GitHub 식 zip(git archive) → 받기 왕복이 맞는지를 본다.
"""
import contextlib
import io
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
os.environ.setdefault("DOGANI_HOME", tempfile.mkdtemp(prefix="dogani-home-"))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import codesign  # noqa: E402
import sign_release  # noqa: E402
import updater  # noqa: E402

KEY = bytes(range(32))
PUB = codesign.ed25519.public_key(KEY).hex()
OTHER = bytes(range(1, 33))
TAG = "v26.11.24.01"
APP_FILES = {"dogani.py": b"print(1)\n", "ui/app.js": b"//\n", "run.bat": b"@echo off\r\nrun\r\n"}


def with_sig(files, tag=TAG, key=KEY):
    return dict(files, **{codesign.SIG_NAME: codesign.make(tag, files, key)})


class CheckTest(unittest.TestCase):
    """codesign.check: 맞으면 None, 무엇이든 다르면 이유 문자열"""

    def check(self, files, tag=TAG):
        return codesign.check(tag, files, (PUB,))

    def test_ok(self):
        self.assertIsNone(self.check(with_sig(APP_FILES)))

    def test_missing_or_broken_sig(self):
        self.assertEqual(self.check(dict(APP_FILES)), "서명 파일 없음")
        for raw in (b"", b"{", b"[]", b'{"tag": 1}', b'{"tag": "v", "key": "zz", "sig": "00"}', b"\xff"):
            self.assertIsNotNone(self.check(dict(APP_FILES, **{codesign.SIG_NAME: raw})), raw)

    def test_changed_added_removed(self):
        signed = with_sig(APP_FILES)
        self.assertIsNotNone(self.check(dict(signed, **{"dogani.py": b"print(2)\n"})))
        self.assertIsNotNone(self.check(dict(signed, **{"evil.py": b"x"})))
        gone = dict(signed)
        del gone["ui/app.js"]
        self.assertIsNotNone(self.check(gone))

    def test_other_tag_is_rejected(self):
        """예전 태그 서명본을 새 태그로 올려도 안 받음 (예전 코드로 되돌리기 방지)"""
        self.assertIn("다른 태그", self.check(with_sig(APP_FILES, tag="v26.11.20.01")))

    def test_unknown_key(self):
        """남의 키로 서명하고 release.sig 의 key 를 그 키로 바꿔도 SIGN_KEYS 에 없으면 안 받음"""
        self.assertEqual(self.check(with_sig(APP_FILES, key=OTHER)), "모르는 서명 키")

    def test_line_endings_and_editor(self):
        """GitHub zip 이 .bat 을 CRLF 로 줘도 같음. editor/ 는 받지 않으니 서명에서도 뺌"""
        signed = with_sig(APP_FILES)
        lf = dict(signed, **{"run.bat": b"@echo off\nrun\n", "editor/x.js": b"anything"})
        self.assertIsNone(self.check(lf))

    def test_binary_line_endings_are_signed(self):
        """그림 같은 바이너리는 \\r 하나만 바뀌어도 안 맞음 (줄바꿈 맞추기는 글자 파일만)"""
        signed = with_sig(dict(APP_FILES, **{"ui/icon.png": b"\x89PNG\r\n\x1a\n"}))
        self.assertIsNone(self.check(signed))
        self.assertIsNotNone(self.check(dict(signed, **{"ui/icon.png": b"\x89PNG\n\x1a\n"})))

    def test_real_keys_are_points(self):
        """updater.SIGN_KEYS 의 공개키가 실제 ed25519 점인지 (잘못 붙여 넣으면 모든 업데이트가 막힘)"""
        self.assertTrue(updater.SIGN_KEYS)
        for k in updater.SIGN_KEYS:
            self.assertTrue(codesign.ed25519._decompress(bytes.fromhex(k)), k)


def make_zip(app, top="dogani-" + TAG):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top}/README.md", "x")
        z.writestr(f"{top}/app/", b"")   # GitHub zip 처럼 폴더 항목도 (빈 이름으로 서명 확인을 깨면 안 됨)
        for rel, data in app.items():
            z.writestr(f"{top}/app/{rel}", data)
    return buf.getvalue()


class StageSignTest(unittest.TestCase):
    """updater.stage: 서명이 안 맞으면 SignatureError 이고 디스크에 아무것도 안 남김"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        self.staged = os.path.join(self.tmp, "app.new")
        self.zip = b""
        self.tag = TAG
        self.downloads = 0

        def get(url, timeout):
            self.downloads += 1
            return self.zip
        for name, value in (("SIGN_KEYS", (PUB,)), ("STAGED_DIR", self.staged),
                            ("REJECTED_FILE", os.path.join(self.tmp, "app.rejected")),
                            ("latest_tag", lambda timeout=5: self.tag),
                            ("current_version", lambda: {"id": "v26.11.20.01"}),
                            ("_get", get)):
            p = mock.patch.object(updater, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def assertNothingStaged(self):
        self.assertFalse(os.path.exists(self.staged))
        self.assertFalse(os.path.exists(self.staged + ".tmp"))

    def test_signed(self):
        self.zip = make_zip(with_sig(APP_FILES))
        self.assertTrue(updater.stage())
        with open(os.path.join(self.staged, "dogani.py"), "rb") as f:
            self.assertEqual(f.read(), APP_FILES["dogani.py"])

    def test_rejected(self):
        bad = dict(with_sig(APP_FILES), **{"dogani.py": b"import os; os.system('x')\n"})
        for app in (dict(APP_FILES), bad, with_sig(APP_FILES, key=OTHER), with_sig(APP_FILES, tag="v26.11.20.01")):
            with contextlib.suppress(FileNotFoundError):
                os.remove(updater.REJECTED_FILE)   # 태그마다 처음 보는 것처럼
            self.zip = make_zip(app)
            with self.assertRaises(updater.SignatureError):
                updater.stage()
            self.assertNothingStaged()

    def test_rejected_tag_is_remembered(self):
        """거절한 태그는 다음에 켤 때 다시 받지 않고(로그도 한 번만), 더 새 태그가 나오면 다시 확인"""
        self.zip = make_zip(dict(APP_FILES))
        with self.assertRaises(updater.SignatureError):
            updater.stage()
        self.assertFalse(updater.stage())
        self.assertEqual(self.downloads, 1)
        self.tag = "v26.11.24.02"
        self.zip = make_zip(with_sig(APP_FILES, tag=self.tag))
        self.assertTrue(updater.stage())

    def test_keeps_staged_update(self):
        """이미 받아 둔 맞는 업데이트(app.new)를 서명 안 맞는 더 새 태그가 지우지 않음"""
        os.makedirs(self.staged)
        with open(os.path.join(self.staged, "version.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.zip = make_zip(dict(APP_FILES))
        with self.assertRaises(updater.SignatureError):
            updater.stage()
        self.assertTrue(os.path.isfile(os.path.join(self.staged, "version.json")))

    def test_backslash_path(self):
        """서명이 있어도 경로에 \\ 나 : 가 있으면 풀지 않음 (Windows 에서 폴더 밖으로 나갈 수 있음)"""
        self.zip = make_zip(with_sig(dict(APP_FILES, **{"ui\\..\\..\\evil.py": b"x"})))
        self.assertFalse(updater.stage())
        self.assertNothingStaged()


class LoadTest(unittest.TestCase):
    def test_loads_by_path_without_sys_modules(self):
        """런처처럼 updater.py 를 경로로 불러도 codesign·ed25519 를 찾고, 이름으로는 올리지 않음
        (올리면 코드를 바꿔 끼운 뒤 dogani 가 예전 ed25519 를 씀)"""
        code = ("import importlib.util, sys, os\n"
                "spec = importlib.util.spec_from_file_location('_boot_updater', os.path.join(sys.argv[1], 'updater.py'))\n"
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                "cs = m._codesign(); assert cs.check('t', {}, ()) == '서명 파일 없음'\n"
                "print(sorted(n for n in ('codesign', 'ed25519') if n in sys.modules))\n")
        out = subprocess.run([sys.executable, "-I", "-c", code, os.path.abspath(APP)], capture_output=True,
                             cwd=tempfile.gettempdir(), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        self.assertEqual(out.stdout.decode().strip(), "[]")


class SignReleaseTest(unittest.TestCase):
    """tools/sign_release.py: 인덱스를 서명 → 커밋 → git archive zip(GitHub 와 같은 변환) → updater.stage 가 받음"""

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
                               "-c", "core.autocrlf=false", "-c", "core.eol=lf", *args],
                              capture_output=True, check=True).stdout

    def write(self, rel, data):
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.write(".gitattributes", b"* text=auto\n*.bat text eol=crlf\n")
        self.write("app/dogani.py", b"print(1)\n")
        self.write("app/run.bat", b"@echo off\nrun\n")
        self.write("app/ui/app.js", b"//\n")
        self.write("app/editor/big.js", b"//\n")
        self.write("app/updater.py", f'SIGN_KEYS = ("{PUB}",)\n'.encode())
        self.write("app/notes.txt", b"not staged\n")   # 커밋 안 되는 파일은 서명에 안 들어가야 함
        self.git("add", ".gitattributes", "app/dogani.py", "app/run.bat", "app/ui/app.js", "app/editor/big.js",
                 "app/updater.py")
        key = os.path.join(self.tmp, "key")
        with open(key, "w", encoding="ascii") as f:
            f.write(KEY.hex() + "\n")
        for target, name, value in ((sign_release, "ROOT", self.repo), (sign_release, "KEY_PATH", key),
                                    (updater, "SIGN_KEYS", (PUB,))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_round_trip(self):
        sign_release.sign(TAG)
        self.assertIn(b"app/release.sig", self.git("diff", "--cached", "--name-only"))
        self.git("commit", "-q", "-m", TAG)
        data = self.git("archive", "--format=zip", f"--prefix=dogani-{TAG}/", "HEAD")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn(b"\r\n", z.read(f"dogani-{TAG}/app/run.bat"))   # 줄바꿈이 바뀌어 나오는 경우를 실제로 시험
        staged = os.path.join(self.tmp, "app.new")
        with mock.patch.object(updater, "STAGED_DIR", staged), \
                mock.patch.object(updater, "latest_tag", lambda timeout=5: TAG), \
                mock.patch.object(updater, "current_version", lambda: {"id": "v26.11.20.01"}), \
                mock.patch.object(updater, "_get", lambda url, timeout: data):
            self.assertTrue(updater.stage())
        self.assertTrue(os.path.isfile(os.path.join(staged, "dogani.py")))

    def refuses(self, tag=TAG):
        with self.assertRaises(SystemExit):
            sign_release.sign(tag)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "app", codesign.SIG_NAME)))

    def test_refuses_bad_tag_or_unstaged_key(self):
        """태그 모양이 아니거나, 스테이징된 updater.py 의 SIGN_KEYS 에 키가 없으면 서명하지 않음
        (작업 폴더에만 키를 더하고 add 를 안 했으면 커밋된 코드가 모르는 키)"""
        self.refuses("v1")
        self.git("rm", "-q", "--cached", "app/updater.py")
        self.refuses()
        self.git("add", "app/updater.py")
        self.write("app/updater.py", b'SIGN_KEYS = ("00",)\n')
        self.git("add", "app/updater.py")
        self.write("app/updater.py", f'SIGN_KEYS = ("{PUB}",)\n'.encode())   # 작업 폴더만 고침
        self.refuses()

    def test_previous_tag_keys(self):
        """직전 태그(동료 PC 가 지금 돌리는 코드)가 모르는 새 키로는 서명하지 않음. 서명 도입 전 태그는 상관없음"""
        self.git("commit", "-q", "-m", "old")
        self.git("tag", "v26.11.20.01")   # 이 태그의 updater.py 는 PUB 을 앎
        self.write("app/updater.py", f'SIGN_KEYS = ("{PUB}", "{"ab" * 32}")\n'.encode())
        self.git("add", "app/updater.py")
        sign_release.sign(TAG)
        os.remove(os.path.join(self.repo, "app", codesign.SIG_NAME))
        self.git("rm", "-q", "--cached", "app/" + codesign.SIG_NAME)

        self.write("app/updater.py", b'SIGN_KEYS = ("' + b"ab" * 32 + b'",)\n')
        self.git("add", "app/updater.py")
        self.git("commit", "-q", "-m", "other key")
        self.git("tag", "v26.11.21.01")   # 직전 태그가 다른 키만 앎
        self.write("app/updater.py", f'SIGN_KEYS = ("{PUB}",)\n'.encode())
        self.git("add", "app/updater.py")
        self.refuses()

        self.git("tag", "-d", "v26.11.21.01")
        self.git("tag", "-d", "v26.11.20.01")
        self.write("app/updater.py", b"# no keys yet\n")
        self.git("add", "app/updater.py")
        self.git("commit", "-q", "-m", "pre")
        self.git("tag", "v26.11.19.01")   # 서명 도입 전 = 확인 없이 받음
        self.write("app/updater.py", f'SIGN_KEYS = ("{PUB}",)\n'.encode())
        self.git("add", "app/updater.py")
        sign_release.sign(TAG)

    def test_refuses_conflict(self):
        """합치기 충돌 중이면 서명하지 않음"""
        with mock.patch.object(sign_release, "git", side_effect=lambda *a: (
                b"100644 " + b"0" * 40 + b" 1\tapp/dogani.py\0" if a[0] == "ls-files" else self.git(*a))):
            with self.assertRaises(SystemExit):
                sign_release.staged_app()


if __name__ == "__main__":
    unittest.main()
