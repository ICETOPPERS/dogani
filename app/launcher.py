"""
dogani.exe 진입점 (런처). 이 파일만 exe 에 고정되고, 실제 프로그램 코드(dogani.py, ui/ ...)는
%APPDATA%\\dogani\\app 에 받아 둔 최신본을 실행한다. 받은 게 없거나 깨졌으면 exe 에 같이 넣은 기본 코드로 실행.

켤 때마다 (디스코드식)
  1) 지금 코드의 updater.py 로 GitHub 최신 태그를 확인해 새 버전이면 app.new 에 받고
  2) app.new 를 app 으로 바꿔 끼운 뒤
  3) 실행할 코드 폴더를 고른다.  → 받는 방법은 updater.py 에 있어서 런처는 거의 바뀔 일이 없다.

받은 코드가 import 나 시작 중에 죽으면 그 버전을 app.bad 에 적고 내장 코드로 실행한다.
창 없는 exe 라 오류가 화면에 안 남으므로 launcher.log 에 적는다.
"""
import ctypes
import datetime
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

import deps  # noqa: F401  PyInstaller 가 PySide6 등 의존성을 exe 에 넣도록

CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dogani")
CACHE_DIR = os.path.join(CONFIG_DIR, "app")
STAGED_DIR = CACHE_DIR + ".new"
BAD_FILE = CACHE_DIR + ".bad"  # 시작하다 죽은 버전 id. 같은 버전을 또 받아도 바꿔 끼우지 않음
LOG_FILE = os.path.join(CONFIG_DIR, "launcher.log")
LOG_MAX = 256 * 1024
SAFE_ENV = "DOGANI_SAFE"  # 내장 코드로 다시 띄운 프로세스 표시 (업데이트를 건너뛰고 바로 실행)
BUNDLED_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "app")


def log(msg):
    """한 줄 + (예외 중이면) traceback. 커지면 새로 씀. 로그 실패로 실행을 막지 않음"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            mode = "w" if os.path.getsize(LOG_FILE) > LOG_MAX else "a"
        except OSError:
            mode = "a"
        tb = traceback.format_exc() if sys.exc_info()[0] else ""
        with open(LOG_FILE, mode, encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n{tb}")
    except Exception:
        pass


def already_running():
    """dogani.single_instance() 와 같은 mutex. 이미 떠 있으면 그 프로세스가 쓰는 코드 폴더를 건드리면 안 됨"""
    handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\DoganiMutex")  # SYNCHRONIZE
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
    return bool(handle)


def version_id(folder):
    try:
        with open(os.path.join(folder, "version.json"), encoding="utf-8") as f:
            vid = json.load(f).get("id", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return vid if isinstance(vid, str) else ""  # 숫자·null 이면 버전 비교에서 TypeError


def bad_version():
    try:
        with open(BAD_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_bad(vid):
    try:
        if vid:
            with open(BAD_FILE, "w", encoding="utf-8") as f:
                f.write(vid)
        elif os.path.exists(BAD_FILE):
            os.remove(BAD_FILE)
    except OSError:
        pass


def mark_bad(folder):
    """시작하다 죽은 받은 코드의 버전을 기억 → code_dir 이 건너뛰고, 같은 태그는 다시 받지 않음.
    폴더는 지우지 않음: 원인이 코드가 아니라 환경이면(내장 코드도 죽으면) 표시만 풀어 다시 쓰려고.
    버전을 모르면 기억할 수 없으니 지움"""
    vid = version_id(folder)
    write_bad(vid)
    if not vid:
        shutil.rmtree(folder, ignore_errors=True)


def apply_staged():
    if not os.path.isfile(os.path.join(STAGED_DIR, "version.json")):  # 다 받은 것만 (version.json 은 마지막에 씀)
        return
    bad = bad_version()
    if bad and version_id(STAGED_DIR) == bad:  # 지난번에 시작하다 죽은 버전
        shutil.rmtree(STAGED_DIR, ignore_errors=True)
        return
    try:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        os.replace(STAGED_DIR, CACHE_DIR)
    except OSError:
        pass


def code_dir():
    """받은 코드와 exe 기본 코드 중 새 버전 (exe 를 새로 빌드해 나눠 주면 기본 코드가 더 새것일 수 있음)"""
    vid = version_id(CACHE_DIR)
    if os.path.isfile(os.path.join(CACHE_DIR, "dogani.py")) and vid != bad_version() and vid >= version_id(BUNDLED_DIR):
        return CACHE_DIR
    return BUNDLED_DIR


def fetch_update():
    """실행할 코드의 updater.stage() 로 새 버전을 받아 둔다. 실패해도 그냥 지금 버전으로 실행"""
    try:
        spec = importlib.util.spec_from_file_location("_boot_updater", os.path.join(code_dir(), "updater.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bad = bad_version()
        if bad > mod.current_version().get("id", ""):
            # 죽은 버전을 이미 가진 것으로 쳐서 stage() 가 같은 태그 zip 을 켤 때마다 또 받지 않게
            mod.current_version = lambda: {"id": bad, "label": bad}
        mod.stage()
    except Exception as e:   # 이유를 한 줄에 (서명 거절인지 네트워크 탓인지 traceback 끝까지 안 봐도 알게)
        log(f"업데이트 확인 실패: {e}")


def drop_modules(folder):
    """folder 에서 올라온 모듈을 모두 내림. 이름 목록을 따로 두면 새 모듈이 생길 때 빠져서
    반쯤 올라온 받은 코드 모듈이 내장 코드 import 에 그대로 잡힘"""
    root = os.path.normcase(os.path.abspath(folder)) + os.sep
    for name, mod in list(sys.modules.items()):
        path = getattr(mod, "__file__", None)
        if path and os.path.normcase(os.path.abspath(path)).startswith(root):
            sys.modules.pop(name, None)
    while folder in sys.path:
        sys.path.remove(folder)


def run(code_dir):
    sys.path.insert(0, code_dir)
    os.environ["DOGANI_CODE_DIR"] = code_dir
    # import dogani 로 쓰면 PyInstaller 가 dogani 를 exe 안에 굳혀 넣어서 받은 코드보다 먼저 잡힌다
    return importlib.import_module("dogani").main


def restart_bundled():
    """시작 중에 죽은 받은 코드는 Qt 앱·트레이 스레드·mutex 를 이미 잡았을 수 있어 같은 프로세스에서
    내장 코드를 다시 못 띄움 → 새 프로세스로 띄우고 이 프로세스는 바로 끝냄 (mutex 는 OS 가 풀어 줌)"""
    cmd = [sys.executable] + ([] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]) + sys.argv[1:]
    try:
        subprocess.Popen(cmd, env=dict(os.environ, **{SAFE_ENV: "1"}))
    except OSError:
        log("내장 코드로 다시 띄우지 못함")
    os._exit(1)


def wait_parent(timeout=10):
    """restart_bundled 로 뜬 경우 앞 프로세스가 끝나 mutex 가 풀릴 때까지 기다림
    (안 기다리면 dogani 가 '이미 실행 중' 으로 알고 꺼짐)"""
    end = time.monotonic() + timeout
    while already_running():
        if time.monotonic() >= end:
            log("앞 프로세스가 끝나지 않아 그대로 실행")
            return
        time.sleep(0.1)


def run_bundled(safe=False):
    """내장 코드 실행. 여기서도 죽으면 원인을 남김 (창 없는 exe 라 안 남기면 아무것도 안 뜬 채 끝남)"""
    try:
        run(BUNDLED_DIR)()
    except Exception:
        if safe:  # 받은 코드도 내장 코드도 시작하다 죽음 = 코드보다 환경 탓 → 받은 코드 표시를 풀어 다음엔 그걸로
            log("내장 코드도 실행 실패 → 받은 코드 표시를 풂")
            write_bad("")
        else:
            log("내장 코드 실행 실패")
        raise


def main():
    # 꺼내서 지움: 남겨 두면 dogani 가 나중에 스스로 다시 켤 때(liveupdate)도 계속 업데이트를 건너뜀
    safe = os.environ.pop(SAFE_ENV, None)
    if safe:
        wait_parent()
    elif not already_running():  # 두 번째 실행은 dogani 가 알아서 종료하므로 업데이트 안 건드림
        apply_staged()  # 지난번 실행 중에 받아 둔 것
        fetch_update()
        apply_staged()
    # 다시 띄운 쪽은 내장 코드만 (app.bad 를 못 썼어도 또 받은 코드로 죽어 끝없이 다시 띄우지 않게)
    if safe or code_dir() != CACHE_DIR:
        run_bundled(bool(safe))
        return
    try:
        entry = run(CACHE_DIR)
    except Exception:  # 받은 코드가 import 부터 깨졌으면 같은 프로세스에서 기본 코드로
        log(f"받은 코드 import 실패 ({version_id(CACHE_DIR)}) → 내장 코드로 실행")
        drop_modules(CACHE_DIR)
        mark_bad(CACHE_DIR)
        run_bundled()
        return
    try:
        entry()
    except Exception:  # 시작하다 죽음 (SystemExit 은 정상 종료라 여기 안 옴)
        log(f"받은 코드 실행 실패 ({version_id(CACHE_DIR)}) → 내장 코드로 다시 띄움")
        mark_bad(CACHE_DIR)
        restart_bundled()


if __name__ == "__main__":
    main()
