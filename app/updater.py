"""
자동 업데이트 (디스코드식: 켤 때만 적용).

- 배포 = GitHub 에 버전 태그(v26.xx.xx.xx) push. 커밋·push 만으로는 안 나감.
- 받는 쪽(exe, 나·동료 동일): 런처가 켤 때 GitHub 의 최신 태그를 확인해 새 버전이면 받아서 바로 적용.
  실행 중에는 확인하지 않음.
- 소스 실행(개발 PC)은 받지 않음.
- 런처(exe) 자체: 실행 중에 최신 Release 의 dogani-setup.exe 를 받아 두고(stage_launcher),
  다음에 켤 때 stage() 가 조용히 설치한 뒤 이 프로세스를 끝냄(apply_launcher). 설치가 끝나면 설치 파일이 새 exe 를 띄움.
  런처는 이 파일의 stage() 만 부르므로, 이 기능은 예전 런처에도 태그 업데이트만으로 들어감.
- 받은 태그는 배포 서명(codesign.py, SIGN_KEYS)이 맞아야 적용.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import winreg
import zipfile

REPO = "nodogani/dogani"
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(CODE_DIR)
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dogani")
CACHE_DIR = os.path.join(CONFIG_DIR, "app")
STAGED_DIR = CACHE_DIR + ".new"
FROZEN = getattr(sys, "frozen", False)
NO_WINDOW = 0x08000000
TAG_RE = re.compile(r"^v\d{2}\.\d{2}\.\d{2}\.\d{2}$")
UA = {"User-Agent": "dogani-updater"}
# 배포 서명 공개키 (codesign.py). 받은 태그는 이 중 하나로 서명돼야 적용. 키를 바꿀 땐 예전 키로 서명한 태그에서 더함
SIGN_KEYS = ("e1a0c4e34e5bb19ab12829abaf71e591e8bbe65a2e807c26c42971b16ffe0949",)


REJECTED_FILE = CACHE_DIR + ".rejected"   # 서명이 안 맞아 거절한 태그: 더 새 태그가 나올 때까지 켤 때마다 다시 받지 않음


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return ""


def _write_text(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


class SignatureError(Exception):
    """서명이 안 맞는 태그 → 런처가 launcher.log 에 남김 (조용히 False 로 넘기면 안 받는 이유를 모름)"""


def _codesign():
    """런처가 이 파일을 경로로 불러 sys.path 에 이 폴더가 없음 → 같은 폴더 codesign.py 도 경로로, sys.modules 에 안 올림"""
    spec = importlib.util.spec_from_file_location("_updater_codesign", os.path.join(CODE_DIR, "codesign.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

SETUP_DIR = os.path.join(CONFIG_DIR, "setup")  # 받아 둔 설치 파일
SETUP_EXE = os.path.join(SETUP_DIR, "dogani-setup.exe")
SETUP_INFO = os.path.join(SETUP_DIR, "setup.json")  # {"tag", "sha256"}. 설치 파일을 다 받은 뒤 마지막에 씀
SETUP_DONE = os.path.join(CONFIG_DIR, "setup.done")  # 설치를 시도한 릴리스 태그 (같은 설치를 반복하지 않게)
SETUP_LOG = os.path.join(CONFIG_DIR, "setup.log")
SETUP_NAME = "dogani-setup.exe"
# installer.iss 의 AppId. Inno Setup 이 사용자 단위 설치 정보를 여기에 적음
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F4D2B1E-7C3A-4E8F-9B21-D0A9C5E3F781}_is1"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # dogani.py 의 자동 실행과 같은 값
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: 런처가 끝나도 설치는 계속


# ---------------------------------------------------------------- 내 버전
def current_version():
    """지금 실행 중인 코드의 버전 {"id": 태그, "label": 태그}"""
    try:
        with open(os.path.join(CODE_DIR, "version.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"id": "", "label": "개발 중" if not FROZEN else ""}


def version_label():
    """exe = 받은/빌드한 버전, 소스 실행 = 마지막 태그 + 개발 중"""
    if FROZEN:
        return current_version().get("label", "")
    try:
        tag = subprocess.run(["git", "-C", REPO_DIR, "describe", "--tags", "--abbrev=0"], capture_output=True,
                             check=True, creationflags=NO_WINDOW).stdout.decode().strip()
        return f"{tag} 개발 중"
    except (OSError, subprocess.CalledProcessError):
        return "개발 중"


# ---------------------------------------------------------------- 받는 쪽 (GitHub)
def _get(url, timeout):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


TAG_PAGES = 5   # 100개씩. tags API 는 순서를 약속하지 않아 100개가 넘으면 다음 쪽도 봄 (태그 정리로 보통 1쪽)


def latest_tag(timeout=5):
    """GitHub 의 가장 최신 버전 태그. 확인 못 하면 None.
    비인증 API 는 IP 마다 시간당 60회: 켤 때 tags 1회 + 1분 뒤 releases/latest 1회라 사내 NAT 하나에 여럿이어도 넉넉함.
    한도에 걸리면(403) None = 이번엔 업데이트 안 함. 둘째 쪽부터 실패하면 그때까지 본 태그로 (첫 쪽만 보던 예전과 같음)"""
    names = []
    for page in range(1, TAG_PAGES + 1):
        try:
            tags = json.loads(_get(f"https://api.github.com/repos/{REPO}/tags?per_page=100&page={page}", timeout))
        except (OSError, ValueError):
            break
        if not isinstance(tags, list):
            break
        names += [n for n in (str(t.get("name") or "") for t in tags if isinstance(t, dict)) if TAG_RE.match(n)]
        if len(tags) < 100:
            break
    return max(names) if names else None  # vYY.MM.DD.NN 은 문자열 정렬 = 시간순


def stage(timeout=20):
    """최신 태그의 app/ 을 STAGED_DIR 에 받아 둔다. 받았으면 True.
    release.sig 가 SIGN_KEYS 로 그 태그에 서명돼 있지 않으면 SignatureError (받지 않음).
    런처가 켤 때 dogani 보다 먼저 부르는 유일한 함수라 받아 둔 런처 설치도 여기서 시작함"""
    if apply_launcher():
        os._exit(0)  # 설치 파일이 dogani.exe 를 덮어써야 하므로 바로 끝냄 (mutex 도 아직 안 잡았음)
    tag = latest_tag(5)
    if not tag or tag <= current_version().get("id", "") or tag == _read_text(REJECTED_FILE):
        return False
    try:
        data = _get(f"https://codeload.github.com/{REPO}/zip/refs/tags/{tag}", timeout)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            prefix = z.namelist()[0].split("/")[0] + "/app/"  # dogani-v26.../app/...
            cs = _codesign()
            files = {n[len(prefix):]: z.read(n) for n in z.namelist() if n.startswith(prefix)
                     and (cs.wanted(n[len(prefix):]) or n[len(prefix):] == cs.SIG_NAME)}
        if "dogani.py" not in files or any(".." in rel.split("/") or "\\" in rel or ":" in rel for rel in files):
            return False
        # 풀기 전에, 그리고 받아 둔 app.new 를 지우기 전에: 서명 안 맞는 태그가 맞는 업데이트를 밀어내지 않게
        err = cs.check(tag, files, SIGN_KEYS)
        if err:
            _write_text(REJECTED_FILE, tag)
            raise SignatureError(f"{tag}: {err}")
        tmp = STAGED_DIR + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(STAGED_DIR, ignore_errors=True)
        for rel, data in files.items():
            dest = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        # version.json 은 마지막에 (다 받았다는 표시)
        with open(os.path.join(tmp, "version.json"), "w", encoding="utf-8") as f:
            json.dump({"id": tag, "label": tag}, f, ensure_ascii=False)
        os.replace(tmp, STAGED_DIR)
        return True
    except (OSError, ValueError, zipfile.BadZipFile, IndexError):
        return False


# ---------------------------------------------------------------- 런처(exe) 업데이트
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def launcher_version():
    """설치된 exe 의 버전 = 빌드 때 넣은 내장 코드 버전. 설치를 시도한 릴리스 태그가 더 크면 그것
    (릴리스를 빌드 때보다 뒤 태그에 달았으면 내장 버전이 영영 작아서 켤 때마다 다시 설치하게 됨)"""
    bundled = _read_json(os.path.join(getattr(sys, "_MEIPASS", ""), "app", "version.json")).get("id", "")
    try:
        with open(SETUP_DONE, encoding="utf-8") as f:
            done = f.read().strip()
    except OSError:
        done = ""
    return max(v for v in (bundled, done) if isinstance(v, str))


def installed_here():
    """설치 파일로 깐 exe 인지. 폴더째 복사해 쓰는 exe 에 설치를 돌리면 다른 곳에 하나 더 깔림"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            where = winreg.QueryValueEx(key, "InstallLocation")[0]
    except OSError:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
    return isinstance(where, str) and bool(where) and norm(where) == norm(os.path.dirname(sys.executable))


def autostart_on():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "dogani")
            return True
    except OSError:
        return False


def latest_release(timeout=5):
    """최신 Release 의 (태그, 설치 파일 정보). 없거나 모양이 이상하면 None"""
    try:
        rel = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout))
        tag = rel.get("tag_name", "")
        asset = next(a for a in rel.get("assets") or [] if a.get("name") == SETUP_NAME)
    except (OSError, ValueError, AttributeError, TypeError, StopIteration):
        return None
    url, size, digest = asset.get("browser_download_url", ""), asset.get("size"), asset.get("digest") or ""
    if (not isinstance(tag, str) or not TAG_RE.match(tag) or not isinstance(url, str)
            or not url.startswith(f"https://github.com/{REPO}/releases/download/")
            or not isinstance(size, int) or size <= 0 or not isinstance(digest, str)):
        return None
    sha = digest[7:].lower() if digest.startswith("sha256:") else ""  # 예전에 올린 파일은 digest 가 없음
    return tag, {"url": url, "size": size, "sha256": sha}


def _download(url, dest, size, sha256, timeout):
    """임시 파일로 받고 크기·해시가 맞을 때만 dest 로 바꿔 끼움. 받은 파일의 sha256, 실패면 None"""
    tmp = dest + ".tmp"
    h, n = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r, \
                open(tmp, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                h.update(chunk)
                n += len(chunk)
                f.write(chunk)
        if n != size or (sha256 and h.hexdigest() != sha256):
            raise ValueError("크기·해시 불일치")
        os.replace(tmp, dest)
        return h.hexdigest()
    except (OSError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def stage_launcher(timeout=60):
    """최신 Release 의 설치 파일이 지금 exe 보다 새것이면 SETUP_DIR 에 받아 둔다. 받았으면 True.
    150MB 쯤 되므로 켤 때가 아니라 실행 중 백그라운드에서 부름 (liveupdate)"""
    if not FROZEN or not installed_here():
        return False
    staged = _read_json(SETUP_INFO).get("tag")
    if not isinstance(staged, str) or staged <= launcher_version():
        # 설치가 끝났는데 남은 것: 설치 직후 켠 새 exe 가 치우려 할 땐 설치 파일이 아직 돌고 있어 못 지울 수 있음
        shutil.rmtree(SETUP_DIR, ignore_errors=True)
    rel = latest_release()
    if not rel or rel[0] <= launcher_version():
        return False
    tag, asset = rel
    if _read_json(SETUP_INFO).get("tag") == tag and os.path.isfile(SETUP_EXE):
        return False  # 이미 받아 둠
    try:
        os.makedirs(SETUP_DIR, exist_ok=True)
        if os.path.exists(SETUP_INFO):
            os.remove(SETUP_INFO)  # 설치 파일을 바꾸는 동안 예전 표시가 새 파일을 가리키지 않게
    except OSError:
        return False
    sha = _download(asset["url"], SETUP_EXE, asset["size"], asset["sha256"], timeout)
    if not sha:
        return False
    try:
        _write_atomic(SETUP_INFO, json.dumps({"tag": tag, "sha256": sha}))
    except OSError:
        return False
    return True


def setup_command():
    """조용히 설치. 자동 실행은 트레이에서 바꾼 지금 상태를 따름 (설치 파일은 처음 고른 값을 기억하고 있음)"""
    tasks = "autostart" if autostart_on() else "!autostart"
    # /RELAUNCH=1: 설치가 끝나면(실패해도) installer.iss 가 dogani 를 다시 띄움
    return [SETUP_EXE, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL", "/SP-",
            f"/LOG={SETUP_LOG}", f"/MERGETASKS={tasks}", "/RELAUNCH=1"]


def apply_launcher():
    """받아 둔 설치 파일이 지금 exe 보다 새것이면 실행. 실행했으면 True (부른 쪽이 바로 끝나야 함).
    한 태그는 한 번만 시도: 설치가 실패해도 다음에 켤 때는 지금 exe 로 정상 실행"""
    if not FROZEN:
        return False
    tag = _read_json(SETUP_INFO).get("tag")
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        return False
    if tag <= launcher_version():  # 설치 끝 (또는 더 새 exe 를 직접 깔았음) → 치움
        shutil.rmtree(SETUP_DIR, ignore_errors=True)
        return False
    if not installed_here():
        return False
    try:
        if _sha256(SETUP_EXE) != _read_json(SETUP_INFO).get("sha256"):  # 받은 뒤 깨지거나 바뀐 파일
            shutil.rmtree(SETUP_DIR, ignore_errors=True)
            return False
        subprocess.Popen(setup_command(), creationflags=DETACHED, close_fds=True)
    except OSError:  # 못 띄웠으면(백신이 막는 등) 시도로 치지 않음 → 다음에 켤 때 다시
        return False
    try:
        _write_atomic(SETUP_DONE, tag)
    except OSError:
        pass
    return True
