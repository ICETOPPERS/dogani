"""
dogani.py 에 붙이는 라이브 업데이트 (dogani.py 는 install(app) 한 줄만 부름)

- exe: 코드 업데이트는 켤 때 런처가 적용 (디스코드식). 런처 설치 파일만 실행 중에 받아 두고 다음에 켤 때 설치
- 소스 실행(개발 PC): ui 파일 저장 → 창 새로고침, .py 저장 → 재시작
"""
import ctypes
import subprocess
import sys
import threading

from PySide6.QtCore import QObject
from PySide6.QtWebEngineCore import QWebEnginePage

import devreload
import updater

CREATE_NO_WINDOW = 0x08000000
VERSION = updater.version_label()  # 업데이트되면 재시작하므로 한 번만 계산
MUTEX = None  # dogani.main 이 single_instance() 핸들을 넣어 둔다 (재시작 전에 놓아야 새 프로세스가 뜸)


def version_text():
    """트레이 표시용: exe = v26.11.06.02, 소스 실행 = 버전 (최신)"""
    state = getattr(INSTANCE, "state", "") if INSTANCE else ""
    return f"{VERSION} ({state})" if state else VERSION


INSTANCE = None


class LiveUpdate(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.state = "" if getattr(sys, "frozen", False) else "최신"  # 개발 PC 는 배포하는 쪽이라 늘 최신
        if getattr(sys, "frozen", False):
            # 런처 설치 파일은 커서 켤 때 받으면 시작이 늦어짐 → 켜고 1분 뒤 백그라운드로 받아 두고 다음에 켤 때 설치
            t = threading.Timer(60, self.stage_launcher)
            t.daemon = True
            t.start()
        else:
            self.dev = devreload.DevReloader(self)
            self.dev.ui_changed.connect(self.reload_ui)
            self.dev.py_changed.connect(self.restart)

    @staticmethod
    def stage_launcher():
        try:
            updater.stage_launcher()
        except Exception:  # 백그라운드 스레드라 예외가 나도 앱엔 영향 없게
            pass

    def reload_ui(self):
        # 그냥 reload() 는 캐시된 js 를 다시 써서 창을 껐다 켜야 반영됐음 → 캐시 무시 새로고침 (창마다)
        for ov in self.app.windows():
            ov.view.triggerPageAction(QWebEnginePage.WebAction.ReloadAndBypassCache)

    def restart(self):
        # run.bat 으로 실행하면 dogani 는 __main__ 이라 import dogani 하면 한 번 더 로드됨
        dogani = sys.modules.get("dogani") or sys.modules["__main__"]
        app = self.app
        dogani.save_config(app.cfg)
        if app.share:
            app.share.stop()
        app.tray.stop()
        if MUTEX:
            ctypes.windll.kernel32.CloseHandle(MUTEX)
        subprocess.Popen(dogani.launch_command(), creationflags=CREATE_NO_WINDOW)
        app.qapp.quit()


def install(app):
    global INSTANCE
    INSTANCE = app.live_update = LiveUpdate(app)
