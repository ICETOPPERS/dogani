"""
dogani - 스페이스(폴더) 단위 할일·일정 오버레이 + 사내망 팀스페이스

- 화면: 네비게이션 바 하나 + 할일 창 + 달력 창. 바의 이름을 누르면 스페이스(팀스페이스/개인 스페이스)를 고른다
- 개인 스페이스: 이 PC 의 폴더 하나 (스페이스 열기로 바꿈, 남에게 공유 안 함). space.py 참고
- 팀스페이스: 팀 호스트 PC 의 스페이스 (팀원 모두 편집). team.py 참고
- 시스템 트레이(Win32 기본 메뉴)로 표시 / 이름 / 팀 / 스페이스 / 자동 실행 / 종료
"""
import argparse
import ctypes
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes
from urllib.parse import quote

import pystray
from PIL import Image, ImageDraw
from PySide6.QtCore import QBuffer, QByteArray, QFile, QIODevice, QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QGuiApplication, QPalette, QRegion
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript,
                                      QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestJob, QWebEngineUrlScheme,
                                      QWebEngineUrlSchemeHandler)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QVBoxLayout, QWidget)

import holidays
import liveupdate
import space
from server import CLIENT_KEY, HOST_HEADER, KEY_HEADER, TOKEN_HEADER, USER_HEADER, ShareServer
from team import TeamError, TeamHost, clean_name, verify_hello

APP_NAME = "dogani"
CONFIG_DIR = os.environ.get("DOGANI_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dogani")
CONFIG_PATH = os.path.join(CONFIG_DIR, "dogani.json")
TEAM_DIR = os.path.join(CONFIG_DIR, "team")   # 팀 호스트의 서명 키·초대장 (스페이스 id 별)
LEGACY_TEAM_SPACE = os.path.join(TEAM_DIR, "notes")   # v26.11.18 이전 팀 호스트의 기본 폴더 (migrate_spaces 에만 씀)
RECENT_MAX = 10
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # exe 면 런처(launcher.py)가 고른 코드 폴더
UI_DIR = os.path.join(BASE_DIR, "ui")
DEFAULT_PORT = 8765
ME, TEAM = "me", "team"   # v26.11.18 이전 스페이스 키 (설정 옮기기에만 씀)
TEAM_POLL_MS = 20_000

TEAM_STATUS = {"online": "연결됨", "offline": "호스트 연결 끊김", "logged_out": "다시 로그인 필요"}

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    # 스페이스 목록: 내 PC 폴더 {"kind": "local", "id", "dir", "name"} |
    #               다른 PC 에 참여 {"kind": "join", "id", "host", "pc", "token", "mid", "pub", "name"}
    "spaces": [],
    "space": "",          # 할일 창에서 보고 있는 스페이스 id
    "name": "",           # 내 이름 (팀원에게 보이는 이름)
    "cal_hidden": [],     # 달력·할일 일정에서 숨긴 스페이스 id·"#태그" ("#" = 태그 없는 일정)
    "windows": {},        # note / cal -> {"pos", "panel", "open"}
    "wake_click": False,  # True: 다른 프로그램 사용 중엔 굳어 있다가 클릭하면 깨어남 (창은 항상 바탕화면에 고정)
    "theme": "light",
}


# ---------------------------------------------------------------- config
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(space.read_json(CONFIG_PATH) or {})
    # v26.11.17 이전: 네비게이션 바 + "dogani 표시"(전체 숨김) → 트레이에서 할일·달력 창을 하나씩 켬
    cfg["windows"].pop("nav", None)
    if cfg.pop("visible", True) is False:
        for w in cfg["windows"].values():
            w["open"] = False
    if not isinstance(cfg.get("cal_hidden"), list):
        cfg["cal_hidden"] = []
    migrate_spaces(cfg)
    if not isinstance(cfg.get("spaces"), list):
        cfg["spaces"] = []
    cfg["spaces"] = [e for e in cfg["spaces"] if isinstance(e, dict) and e.get("kind") in ("local", "join")
                     and (e.get("dir") if e["kind"] == "local" else e.get("host") and e.get("token"))]
    if not cfg["name"]:
        cfg["name"] = clean_name(socket.gethostname() or os.environ.get("USERNAME"))   # 처음엔 PC 이름
    return cfg


def migrate_spaces(cfg):
    """v26.11.18 이전: 개인 스페이스 하나 + 팀(호스트 또는 참여) 하나 → 스페이스 목록.
    id 는 폴더를 열 때(내 PC) 또는 호스트에 물어서(참여) 채움. legacy = 예전 키(me | team): 보던 스페이스·숨김 옮기기용"""
    # 예전 키는 지우지 않음: 같은 PC 에서 예전 exe 를 켜도(업데이트 전·소스 실행과 번갈아) 예전 그대로 동작. 모두 업데이트된 뒤 지워도 됨
    old = {k: cfg[k] for k in ("space_dir", "space_dirs", "space_names", "team", "team_host", "team_space_dir")
           if k in cfg}
    if not old or cfg.get("spaces"):
        return
    spaces = []
    names = old.get("space_names") if isinstance(old.get("space_names"), dict) else {}
    if old.get("space_dir"):
        d = old["space_dir"]
        spaces.append({"kind": "local", "dir": d, "name": space.space_name(d, names), "legacy": ME})
    t = old.get("team") if isinstance(old.get("team"), dict) else {}
    if old.get("team_host"):
        d = old.get("team_space_dir") or LEGACY_TEAM_SPACE
        if not any(e.get("dir") == d for e in spaces):
            spaces.append({"kind": "local", "dir": d, "name": t.get("team") or "", "legacy": TEAM})
    elif t.get("token") and t.get("host") and not t["host"].startswith("127."):
        spaces.append({"kind": "join", "id": "", "host": t["host"], "pc": t.get("pc", ""), "token": t["token"],
                       "mid": t.get("id", ""), "pub": t.get("pub", ""), "name": t.get("team", ""), "legacy": TEAM})
    cfg["spaces"] = spaces


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def normalize_server(text):
    text = text.strip().removeprefix("http://").rstrip("/")
    if not text:
        return ""
    if ":" not in text:
        text = f"{text}:{DEFAULT_PORT}"
    return text


# ---------------------------------------------------------------- http (팀 호스트와 통신)
def host_proof(addr, pub, sid):
    """토큰 없이 addr 이 공개키 pub 인 스페이스 sid 의 호스트인지 확인 (호스트가 nonce 에 서명) → (확인됨, HTTP 상태, 응답)
    공개키는 누구나 되돌려 줄 수 있으므로 pub 비교가 아니라 서명으로 확인한다"""
    nonce = secrets.token_hex(16)
    status, res = http_json(addr, f"/api/s/{sid}/hello?nonce={nonce}")
    return status == 200 and bool(pub) and verify_hello(pub, nonce, res.get("sig", "")), status, res


def find_host(pc, host, pub, sid):
    """호스트 IP 가 바뀌었을 때: PC 이름으로 새 IP 를 찾아(같은 망) 같은 팀 호스트(서명 확인)면 → 새 주소, 없으면 None"""
    port = host.rsplit(":", 1)[-1]
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(pc, None, socket.AF_INET)}
    except (OSError, UnicodeError):
        return None
    for ip in ips:
        alt = f"{ip}:{port}"
        if alt != host and host_proof(alt, pub, sid)[0]:
            return alt
    return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 사내 프록시를 타지 않게


def http_json(server, path, body=None, token="", name="", timeout=4):
    """→ (HTTP 상태, dict). 연결 실패면 상태 0"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://{server}{path}", data=data, method="POST" if data else "GET")
    req.add_header(KEY_HEADER, CLIENT_KEY)
    req.add_header(HOST_HEADER, socket.gethostname()[:64])
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header(TOKEN_HEADER, token)
    if name:
        req.add_header(USER_HEADER, quote(name))
    def parse(raw):   # 객체가 아닌 JSON·깨진 응답은 빈 dict (호출하는 쪽이 .get 을 그냥 씀)
        try:
            res = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return res if isinstance(res, dict) else {}

    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, parse(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, parse(e.read())
        except OSError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


# ---------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def is_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- desktop pin
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
GWLP_HWNDPARENT = -8
GW_OWNER = 4


def find_desktop_window():
    """바탕화면 아이콘(SHELLDLL_DefView)을 가진 창. Win11 24H2+ 는 Progman, 그 이전은 WorkerW 인 경우가 있다."""
    progman = user32.FindWindowW("Progman", None)
    if progman and user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None):
        return progman
    found = []

    def callback(hwnd, _):
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else progman


def set_owner(hwnd, owner):
    """창의 소유자(owner)를 바꾼다. 바탕화면 창이 소유자면 '바탕화면 보기(Win+D)' 때도 바탕화면 위에 남는다."""
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner or 0)


def get_owner(hwnd):
    return user32.GetWindow(hwnd, GW_OWNER)


# ---------------------------------------------------------------- tray
ICON_COLORS = {"on": (46, 160, 67), "off": (150, 150, 150)}


def make_icon_image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=12, fill=color)
    d.line((18, 33, 28, 44, 47, 21), fill="white", width=7, joint="curve")
    return img


class FreshMenuIcon(pystray.Icon):
    """우클릭 시점에 메뉴를 다시 만들어 항상 최신 상태를 보여준다(트레이 스레드에서 실행)."""

    def _on_notify(self, wparam, lparam):
        if lparam == 0x0205:  # WM_RBUTTONUP
            self._update_menu()
        return super()._on_notify(wparam, lparam)


class Tray(QObject):
    """pystray 는 별도 스레드에서 돈다. 메뉴 클릭은 시그널로 Qt 메인 스레드에 넘긴다."""

    command = Signal(str, object)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.icon = FreshMenuIcon(APP_NAME, make_icon_image(ICON_COLORS["off"]), APP_NAME)
        self.icon.menu = pystray.Menu(lambda: self.build_menu())

    def emit(self, name, arg=None):
        # pystray 는 콜백 인자 개수(0~2개)를 검사하므로 기본값 인자를 쓰면 안 된다
        return lambda icon, item: self.command.emit(name, arg)

    @staticmethod
    def left_text(sec):
        """초대장 남은 시간: 1분 넘으면 분, 1분 이하면 mm:ss"""
        return f"{sec // 60}분 남음" if sec > 60 else f"{sec // 60:02d}:{sec % 60:02d} 남음"

    def local_menu(self, sid):
        """내 PC 스페이스: 초대장 · 이름 · 폴더 / 번호 슬롯(나·팀원·초대장) / 닫기"""
        Item = pystray.MenuItem
        host = self.app.local_host(sid)
        if not host:
            return [Item("폴더를 열지 못했습니다", None, enabled=False),
                    Item("대상 폴더 변경하기...", self.emit("change_dir", sid)),
                    pystray.Menu.SEPARATOR,
                    Item("스페이스 닫기", self.emit("close_space", sid))]
        invites = host.invites()
        items = [Item("새 초대장 만들기", self.emit("new_invite", sid)),
                 Item("이름 바꾸기...", self.emit("rename_space", sid)),
                 Item("탐색기에서 보기", self.emit("open_dir", sid)),
                 Item("대상 폴더 변경하기...", self.emit("change_dir", sid)),
                 pystray.Menu.SEPARATOR]
        n = 0
        for m in host.members():
            n += 1
            if m["admin"]:
                items.append(Item(f"{n}  {m['name']} (나)", None, enabled=False))
                continue
            ro = m["perm"] == "read"
            sub = [Item("편집 허용", self.emit("perm", (sid, m["id"], "write" if ro else "read")),
                        checked=lambda _, ro=ro: not ro),
                   Item("다시 초대", self.emit("reinvite", (sid, m["id"])))]
            sub += [Item(f"다시 초대 {i['code']} 지우기 ({self.left_text(i['left'])})",
                         self.emit("cancel_invite", (sid, i["code"])))
                    for i in invites if i.get("mid") == m["id"]]
            sub.append(Item("내보내기", self.emit("kick", (sid, m["id"]))))
            items.append(Item(f"{n}  {m['name']}", pystray.Menu(*sub)))
        for i in invites:
            if i.get("mid"):
                continue
            n += 1
            items.append(Item(f"{n}  {i['code']} ({self.left_text(i['left'])})",
                              self.emit("ask_cancel_invite", (sid, i["code"]))))
        return items + [pystray.Menu.SEPARATOR, Item("스페이스 닫기", self.emit("close_space", sid))]

    def join_menu(self, sid):
        """다른 PC 스페이스: 연결 상태 · 호스트 주소 / 번호 슬롯(보기만) / 나가기"""
        Item = pystray.MenuItem
        app = self.app
        e = app.entry(sid) or {}
        items = [Item(TEAM_STATUS.get(app.join_status.get(sid, "offline"), ""), None, enabled=False),
                 Item("호스트 주소 바꾸기...", self.emit("change_host", sid)),
                 Item("초대장으로 다시 참여하기...", self.emit("join_space")),
                 pystray.Menu.SEPARATOR]
        for n, m in enumerate(e.get("members") or [], 1):
            me = " (나)" if m.get("id") == e.get("mid") else ""
            items.append(Item(f"{n}  {m.get('name', '')}{me}", None, enabled=False))
        return items + [pystray.Menu.SEPARATOR, Item("스페이스에서 나가기", self.emit("leave_space", sid))]

    def space_list(self):
        """스페이스 제목 + 스페이스 이름들 (각각 설정 하위 메뉴). 보기 전환은 할일 창 위 탭에서 함"""
        Item = pystray.MenuItem
        app = self.app
        items = [Item("스페이스", None, enabled=False)]
        for sp in app.spaces():
            sid = sp["key"]
            sub = (lambda sid=sid: self.local_menu(sid)) if sp["kind"] == "me" else (lambda sid=sid: self.join_menu(sid))
            items.append(Item(sp["name"], pystray.Menu(sub),
                              checked=lambda i, sid=sid: app.current_space()["key"] == sid))
        return items + [Item("스페이스 만들기...", self.emit("create_space"), enabled=bool(app.share)),
                        Item("다른 스페이스에 참여하기...", self.emit("join_space"))]

    def build_menu(self):
        Item = pystray.MenuItem
        app, cfg = self.app, self.app.cfg
        return [
            # 좌클릭 = 기본 항목. 메뉴엔 안 보이고 달력 → 할일 → 둘 다 → 닫기 순으로 돌림
            Item("창 순환", self.emit("cycle_panels"), default=True, visible=False),
            Item("할일 표시", self.emit("toggle_panel", "note"), checked=lambda i: "note" in app.panels),
            Item("달력 표시", self.emit("toggle_panel", "cal"), checked=lambda i: "cal" in app.panels),
            pystray.Menu.SEPARATOR,
            Item(f"내 이름: {cfg['name']}", self.emit("rename")),
            pystray.Menu.SEPARATOR,
            *self.space_list(),
            pystray.Menu.SEPARATOR,
            Item("클릭 시 깨어나기", self.emit("toggle_wake"), checked=lambda i: cfg["wake_click"]),
            Item("다크 테마", self.emit("toggle_theme"), checked=lambda i: cfg["theme"] == "dark"),
            Item("Windows 시작 시 자동 실행", self.emit("toggle_autostart"), checked=lambda i: is_autostart()),
            pystray.Menu.SEPARATOR,
            Item(lambda i: liveupdate.version_text(), None, enabled=False),
            Item("종료", self.emit("quit")),
        ]

    def update(self):
        share = self.app.share
        self.icon.icon = make_icon_image(ICON_COLORS["on" if share else "off"])
        if share:
            self.icon.title = f"dogani {liveupdate.version_text()} - {self.app.current_space()['name']}"
        else:
            self.icon.title = f"dogani - 포트 {self.app.cfg['port']} 사용 중 (스페이스를 열 수 없음)"

    def notify(self, text):
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:
            pass

    def start(self):
        self.icon.run_detached()
        self.icon.visible = True

    def stop(self):
        self.icon.stop()


# ---------------------------------------------------------------- dialogs (Windows 기본 위젯)
class TeamDialog(QDialog):
    """팀 참여 (호스트 주소 + 초대장 코드). 이름은 '내 이름'을 그대로 보냄"""

    def __init__(self, host="", name=""):
        super().__init__(None, Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("팀 참여")
        form = QFormLayout()
        self.host = QLineEdit(host, placeholderText="예) 192.168.0.10  (관리자에게 받기)")
        self.code = QLineEdit(placeholderText="XXXX-XXXX-XXXX", maxLength=20)
        form.addRow("호스트 주소", self.host)
        form.addRow("초대장 코드", self.code)
        name_label = QLabel(f"{name}   (트레이 > 내 이름 에서 바꿈)")
        name_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("내 이름", name_label)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("초대장은 스페이스 주인이 사람마다 따로 만들어 줍니다.\n"
                                "15분 안에 한 번만 쓸 수 있습니다. PC 를 바꿨으면 주인에게 다시 초대를 부탁해 주세요."))
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.handler = None
        self.setMinimumWidth(380)

    def check(self):
        if not normalize_server(self.host.text()):
            return self.error.setText("호스트 주소를 입력하세요.")
        if not self.code.text().strip():
            return self.error.setText("초대장 코드를 입력하세요.")
        msg = self.handler(self) if self.handler else None
        if msg:
            self.error.setText(msg)
        else:
            self.accept()


# ---------------------------------------------------------------- 저장 못 한 노트 보관 (끊김 대비)
UNSAVED_PATH = os.path.join(CONFIG_DIR, "unsaved.json")
_unsaved_lock = threading.Lock()


def load_unsaved():
    try:
        with open(UNSAVED_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_unsaved(key, value):
    """key(스페이스 키|날짜) 의 보관 내용 저장(value = JSON) / 지움(빈 값). 오래된 것부터 50개만 남김"""
    with _unsaved_lock:
        d = load_unsaved()
        if value:
            try:
                d[key] = json.loads(value)
            except ValueError:
                return
        elif d.pop(key, None) is None:
            return
        if len(d) > 50:
            for k in sorted(d, key=lambda k: (d[k] or {}).get("at", 0) if isinstance(d[k], dict) else 0)[:len(d) - 50]:
                del d[k]
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = UNSAVED_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, UNSAVED_PATH)
        except OSError:
            pass


# ---------------------------------------------------------------- overlay web view
class Bridge(QObject):
    """JS(app.js) ↔ 파이썬 연결 객체 (창마다 하나)."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    @Slot(result=str)
    def getConfig(self):
        return json.dumps(self.win.js_config())

    @Slot()
    def startMove(self):
        self.win.windowHandle().startSystemMove()

    @Slot(int)
    def startResize(self, edges):
        self.win.start_resize(edges)

    @Slot(str, str)
    def setStatus(self, key, status):
        if status in ("online", "offline", "blocked"):
            self.win.app.on_status(key, status)

    @Slot(str)
    def selectSpace(self, key):
        self.win.app.select_space(key)

    @Slot(str)
    def openNote(self, date):
        self.win.app.open_note(date)

    @Slot(str, int, str)
    def openTask(self, date, line, raw):
        self.win.app.open_task(date, line, raw)

    @Slot(str, str, bool)
    def noteDateChanged(self, date, title, is_today):
        self.win.app.note_date_changed(date, title, is_today)

    @Slot(str)
    def setCalHidden(self, keys_json):
        try:
            keys = json.loads(keys_json)
        except ValueError:
            return
        if isinstance(keys, list):
            self.win.app.set_cal_hidden(keys)

    @Slot(result=str)
    def getUnsaved(self):
        return json.dumps(load_unsaved())

    @Slot(str, str)
    def setUnsaved(self, key, value):
        save_unsaved(key, value)

    @Slot(str)
    def copyText(self, text):
        QGuiApplication.clipboard().setText(text)

    @Slot(str)
    def setCalDrag(self, data):
        """할일 창에서 달력에 놓을 수 있는 줄(할일·장식) 끌기 시작/끝 → 달력 창에 그 데이터 전달.
        장식(일반 글자 줄)은 text/plain 에 원문만 실리므로 놓을 때 이 데이터가 필요함 (bool 로 넘기면 장식이 안 붙음)"""
        cal = self.win.app.panels.get("cal")
        if cal:
            cal.call("setCalDrag", data or "")

    @Slot(str, str)
    def inviteFor(self, name, keys_json):
        """달력 멤버 칩 우클릭: 스페이스에 아직 없는 이름 → 내 PC 스페이스 초대장 (keys = 그 이름 일정이 있는 스페이스 먼저)"""
        try:
            keys = [k for k in json.loads(keys_json) if isinstance(k, str)]
        except ValueError:
            return
        QTimer.singleShot(0, lambda: self.win.app.invite_for(name, keys))

    @Slot(int, result=str)
    def getHolidays(self, year):
        """공휴일 {days, pending} — pending 이면 JS 가 조금 뒤 다시 물음"""
        return json.dumps(holidays.get(CONFIG_DIR, year), ensure_ascii=False)

    @Slot(str)
    def openUrl(self, url):
        if url.startswith(("http://", "https://", "mailto:")):
            QDesktopServices.openUrl(QUrl(url))


# file:// 로 열면 서버(http) 접근이 막히고, LocalContentCanAccessRemoteUrls 를 켜면 로컬 css/js 로딩이 깨진다.
# 그래서 UI 파일은 전용 스킴(dogani://ui/...)으로 제공한다.
UI_SCHEME = b"dogani"
MIME_TYPES = {".html": b"text/html", ".js": b"text/javascript", ".css": b"text/css",
              ".svg": b"image/svg+xml", ".png": b"image/png"}


def register_ui_scheme():
    """QApplication 생성 전에 호출해야 한다."""
    scheme = QWebEngineUrlScheme(UI_SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.CorsEnabled)
    QWebEngineUrlScheme.registerScheme(scheme)


class UiSchemeHandler(QWebEngineUrlSchemeHandler):
    def requestStarted(self, job):
        rel = job.requestUrl().path().lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(UI_DIR, rel))
        mime = MIME_TYPES.get(os.path.splitext(path)[1].lower())
        if not path.startswith(UI_DIR + os.sep) or not mime or not os.path.isfile(path):
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        with open(path, "rb") as f:
            data = f.read()
        buf = QBuffer(job)
        buf.setData(QByteArray(data))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime, buf)


class ClientHeaders(QWebEngineUrlRequestInterceptor):
    """서버로 가는 모든 요청(fetch, SSE)에 dogani 식별 헤더 + 내 이름 + 팀원 토큰을 붙인다."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.host = socket.gethostname().encode("utf-8", "ignore")[:64]

    def interceptRequest(self, info):
        if info.requestUrl().scheme() in ("http", "https"):
            cfg = self.app.cfg
            info.setHttpHeader(KEY_HEADER.encode(), CLIENT_KEY.encode())
            info.setHttpHeader(HOST_HEADER.encode(), self.host)
            info.setHttpHeader(USER_HEADER.encode(), quote(cfg["name"]).encode())
            # 토큰은 그 스페이스의 호스트 주소·경로로 가는 요청에만 (다른 스페이스·다른 PC 로 새지 않게)
            url = info.requestUrl()
            addr, path = f"{url.host()}:{url.port()}".lower(), url.path()   # QUrl 은 호스트 이름을 소문자로 줌
            for e in cfg["spaces"]:
                if (e["kind"] == "join" and e.get("id") and e["host"].lower() == addr
                        and path.startswith(f"/api/s/{e['id']}/")):
                    info.setHttpHeader(TOKEN_HEADER.encode(), e["token"].encode())
                    break


class Page(QWebEnginePage):
    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        # 오버레이 안에서 다른 페이지로 이동하지 않도록 막는다
        if url.scheme() == UI_SCHEME.decode():
            return True
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
        return False


def qwebchannel_js():
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("qwebchannel.js 를 찾을 수 없습니다")
    data = bytes(f.readAll()).decode("utf-8")
    f.close()
    return data


def setup_profile(app):
    """모든 창이 같이 쓰는 기본 프로필에 UI 스킴과 헤더를 한 번만 설치."""
    profile = QWebEngineProfile.defaultProfile()
    app.scheme_handler = UiSchemeHandler(app)
    profile.installUrlSchemeHandler(UI_SCHEME, app.scheme_handler)
    app.headers = ClientHeaders(app)
    profile.setUrlRequestInterceptor(app.headers)
    script = QWebEngineScript()
    script.setName("qwebchannel")
    script.setSourceCode(qwebchannel_js())
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    profile.scripts().insert(script)


PANEL_DEFAULT_SIZE = {"note": (440, 640), "cal": (400, 440)}
PANEL_MIN_SIZE = {"note": (300, 240), "cal": (340, 300)}


class ResizeGhost(QWidget):
    """크기 조절 중에 보여주는 외곽선.
    반투명 창 + 웹뷰는 크기가 바뀔 때마다 화면 전체를 다시 그려서 끌 때 버벅이므로
    (Windows 기본 크기 조절도 마찬가지), 끄는 동안은 이 선만 움직이고 놓을 때 한 번만 실제 창을 바꾼다."""

    BORDER = 2

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#2f80ed"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        b = self.BORDER
        r = self.rect()
        self.setMask(QRegion(r).subtracted(QRegion(r.adjusted(b, b, -b, -b))))


VK_LBUTTON = 0x01
WM_MOUSEACTIVATE, MA_ACTIVATEANDEAT = 0x0021, 2
WM_WINDOWPOSCHANGING, SWP_NOZORDER, SWP_SHOWWINDOW = 0x0046, 0x0004, 0x0040
HWND_BOTTOM = 1


class WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("insert_after", wintypes.HWND), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class Overlay(QWidget):
    """바탕화면 창. mode = note(할일 창) | cal(달력 창). 열고 닫기는 트레이 메뉴"""

    def __init__(self, app, mode):
        super().__init__()
        self.app = app
        self.mode = mode

        self.setWindowTitle(APP_NAME + {"note": " 할일", "cal": " 달력"}[mode])
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.apply_flags()

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        page = Page(self.view)
        page.setBackgroundColor(Qt.GlobalColor.transparent)
        self.view.setPage(page)

        self.bridge = Bridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("bridge", self.bridge)
        page.setWebChannel(self.channel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        w, h = self.wcfg.get("panel") or PANEL_DEFAULT_SIZE[mode]
        mw, mh = PANEL_MIN_SIZE[mode]
        self.setMinimumSize(mw, mh)
        self.resize(max(w, mw), max(h, mh))
        self.restore_position()
        # 탐색기 재시작 등으로 바탕화면 창이 바뀌면 다시 고정
        self.pin_timer = QTimer(self, interval=5000, timeout=self.apply_pin)
        self.pin_timer.start()
        self._loaded = False
        self._want_show = False
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl("dogani://ui/index.html"))

    def reveal(self):
        """창 보이기. 처음 열 때는 페이지가 그려진 뒤에 보여서, 빈 흰 창이 잠깐 비치지 않게 함"""
        if self._loaded or self.isVisible():
            self.show_pinned()
            return
        self._want_show = True
        QTimer.singleShot(2000, self._on_loaded)   # 불러오기가 늦어도 창은 뜨게

    def _on_loaded(self, *_):
        if self._loaded:
            return
        self._loaded = True
        if self._want_show:
            QTimer.singleShot(60, self.show_pinned)   # 첫 화면이 그려질 틈

    def show_pinned(self):
        """바탕화면 소유로 먼저 묶은 뒤 활성화 없이 표시 → 다른 창 위로 잠깐 떠오르지 않음"""
        if self.isVisible():
            return
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        set_owner(int(self.winId()), find_desktop_window())
        self.show()

    def nativeEvent(self, event_type, message):
        # 다른 앱을 쓰다 바탕화면의 dogani 를 처음 누르면 창만 깨우고 그 클릭은 버림
        # → 지나가다 누른 클릭에 체크박스·링크가 바로 눌리지 않게 (두 번째 클릭부터 동작)
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if (msg.message == WM_MOUSEACTIVATE and self.app.cfg["wake_click"]
                    and self.app.qapp.applicationState() != Qt.ApplicationState.ApplicationActive):
                return True, MA_ACTIVATEANDEAT
            if msg.message == WM_WINDOWPOSCHANGING:
                pos = WINDOWPOS.from_address(msg.lParam)
                if pos.flags & SWP_SHOWWINDOW:
                    # 처음 표시될 때부터 맨 아래(바탕화면 바로 위)에 놓음: 잠깐 맨 위로 떴다 내려가지 않게
                    pos.insert_after = HWND_BOTTOM
                    pos.flags &= ~SWP_NOZORDER
                elif self.app.cfg["wake_click"]:
                    # 바탕화면 고정: 눌러도 다른 창 위로 올라오지 않게 (깨어나기만 함)
                    pos.flags |= SWP_NOZORDER
        return super().nativeEvent(event_type, message)

    @property
    def wcfg(self):
        """이 창의 위치·크기 설정"""
        return self.app.win_cfg(self.mode)

    def js_config(self):
        cfg = self.app.js_common()
        cfg["mode"] = self.mode
        if self.mode == "note":
            cfg["date"] = self.wcfg.pop("date", None)
        return cfg

    # -- window flags
    def apply_flags(self):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()

    def showEvent(self, event):
        super().showEvent(event)
        # Qt 가 표시 직후 소유자를 다시 설정하므로 조금 뒤에 적용
        QTimer.singleShot(300, self.apply_pin)

    def apply_pin(self):
        """바탕화면 창을 소유자로 지정 (Win+D 에도 남고 다른 창 위로 올라오지 않음)"""
        if not self.isVisible():
            return
        hwnd = int(self.winId())
        target = find_desktop_window()
        if (get_owner(hwnd) or None) != (target or None):
            set_owner(hwnd, target)

    def place_on_saved_screen(self):
        """모니터별로 기억한 위치(작업 영역 기준 상대 좌표)로 옮김. 기억한 모니터가 없으면 주 모니터의 같은 상대 위치."""
        rel = self.wcfg.get("screens") or {}
        key = self.wcfg.get("screen")
        if not key or key not in rel:
            return False
        screens = {screen_key(s): s for s in QGuiApplication.screens()}
        screen = screens.get(key) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        rx, ry = rel[key] if key in screens else rel.get(screen_key(screen), rel[key])
        self._placing = True
        try:
            self.move(geo.left() + rx, geo.top() + ry)
            self.keep_on_screen(QPoint(geo.left() + 1, geo.top() + 1) if self._off_screen() else None)
        finally:
            self._placing = False
        return True

    def _off_screen(self):
        return QGuiApplication.screenAt(self.pos() + QPoint(30, 20)) is None

    def restore_position(self):
        if self.place_on_saved_screen():
            return
        pos = self.wcfg.get("pos")
        if pos and QGuiApplication.screenAt(QPoint(pos[0] + 30, pos[1] + 20)):
            self.move(pos[0], pos[1])
            self.keep_on_screen()
            return
        # 처음 여는 창: 주 모니터 오른쪽 위. 할일 창은 맨 오른쪽, 달력 창은 그 왼쪽
        geo = QGuiApplication.primaryScreen().availableGeometry()
        x = geo.right() - PANEL_DEFAULT_SIZE["note"][0] - 20
        if self.mode == "cal":
            x -= self.width() - 10
        self.move(max(geo.left(), x), geo.top() + 20)
        self.keep_on_screen(self.pos())

    # -- size
    def start_resize(self, edges):
        """가장자리 크기 조절 시작. edges = Qt.Edge 비트 (위1 왼2 오4 아래8, app.js .rz data-e)"""
        if getattr(self, "_ghost", None) is None:
            self._ghost = ResizeGhost()
            self._resize_timer = QTimer(self, interval=15, timeout=self._resize_tick)
        self._resize_from = (QCursor.pos(), self.geometry(), edges)
        self._resize_geo = self.geometry()
        self._ghost.setGeometry(self._resize_geo)
        self._ghost.show()
        self._resize_timer.start()

    def _resize_tick(self):
        start, geo, edges = self._resize_from
        d = QCursor.pos() - start
        mn = self.minimumSize()
        l, t, r, b = geo.left(), geo.top(), geo.right(), geo.bottom()
        # 왼쪽·위를 끌면 반대쪽 끝은 고정, 최소 크기에서 멈춤
        if edges & 2:
            l = min(l + d.x(), r - mn.width() + 1)
        if edges & 4:
            r = max(r + d.x(), l + mn.width() - 1)
        if edges & 1:
            t = min(t + d.y(), b - mn.height() + 1)
        if edges & 8:
            b = max(b + d.y(), t + mn.height() - 1)
        self._resize_geo = QRect(QPoint(l, t), QPoint(r, b))
        self._ghost.setGeometry(self._resize_geo)
        if not ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            self._resize_timer.stop()
            self._ghost.hide()
            self.setGeometry(self._resize_geo)

    def keep_on_screen(self, anchor=None):
        anchor = anchor if anchor is not None else self.pos()
        screen = QGuiApplication.screenAt(anchor + QPoint(30, 20)) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        # 큰 화면(RDP QHD 등)에서 키운 창이 작은 화면에서 아래·오른쪽이 잘려 가장자리를 못 잡지 않게 줄임
        if self.width() > geo.width() or self.height() > geo.height():
            self.resize(min(self.width(), geo.width()), min(self.height(), geo.height()))
        x = min(self.x(), geo.right() - self.width() + 1)
        y = min(self.y(), geo.bottom() - self.height() + 1)
        self.move(max(geo.left(), x), max(geo.top(), y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.wcfg["panel"] = [self.width(), self.height()]
        self.app.save_later()

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, "_placing", False) or self.app.screens_changing:
            return  # 모니터 연결/해제로 밀려난 이동은 기억하지 않음
        self.wcfg["pos"] = [self.x(), self.y()]
        screen = QGuiApplication.screenAt(self.pos() + QPoint(30, 20))
        if screen:
            geo = screen.availableGeometry()
            key = screen_key(screen)
            self.wcfg.setdefault("screens", {})[key] = [self.x() - geo.left(), self.y() - geo.top()]
            self.wcfg["screen"] = key
        self.app.save_later()

    def push_config(self, **values):
        self.view.page().runJavaScript(f"window.App && App.configure({json.dumps(values)})")

    def call(self, fn, *args):
        self.view.page().runJavaScript(f"window.App && App.{fn}({', '.join(json.dumps(a) for a in args)})")


# ---------------------------------------------------------------- app
def overlaps(a, b):
    """두 폴더가 같거나 한쪽이 다른 쪽 안에 있음"""
    a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
    try:
        common = os.path.commonpath([a, b])
    except ValueError:   # 다른 드라이브
        return False
    return common in (a, b)


def screen_key(screen):
    """모니터 식별자: 연결 순서에 따라 바뀌는 DISPLAYn 대신 제조사·모델·일련번호 우선."""
    def base(s):
        return " ".join(p for p in (s.manufacturer(), s.model(), s.serialNumber()) if p).strip()
    key = base(screen)
    if not key:
        return screen.name()
    # 같은 모델 모니터 여러 대(일련번호 없음)면 키가 겹치므로 DISPLAYn 을 붙여 구분
    if sum(1 for s in QGuiApplication.screens() if base(s) == key) > 1:
        key = f"{key} {screen.name()}"
    return key


class doganiApp(QObject):
    team_result = Signal(int, object)   # 팀 호스트 응답 (작업 스레드 → 메인 스레드)
    bg_result = Signal(object)          # run_bg 결과 (done, 결과)

    def __init__(self, qapp, cfg):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg
        qapp.applicationStateChanged.connect(self.push_awake)
        self.status = {}     # 스페이스 키 -> 창에서 알려 준 연결 상태
        self.panels = {}     # "note" | "cal" -> Overlay (트레이에서 켜고 끔)
        self.join_status = {}   # 참여 스페이스 id -> online | offline | logged_out
        self.polling = set()    # 확인 중인 참여 스페이스 (id, id 모르면 토큰)
        self.save_timer = QTimer(self, singleShot=True, interval=800, timeout=lambda: save_config(self.cfg))
        # 모니터 연결/해제: Windows 가 창을 밀어 놓은 뒤 각 창을 기억한 모니터 기준 위치로 되돌림
        self.screens_changing = False
        self.screen_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.on_screens_settled)
        qapp.screenAdded.connect(self.on_screens_changed)
        qapp.screenRemoved.connect(self.on_screens_changed)
        qapp.primaryScreenChanged.connect(self.on_screens_changed)
        # RDP 로 접속했다 로컬로 돌아오면 모니터는 그대로고 해상도만 바뀜 → 이것도 다시 맞춤
        qapp.screenAdded.connect(self.watch_screen)
        for s in qapp.screens():
            self.watch_screen(s)
        setup_profile(self)

        self.share = ShareServer(cfg["port"], cfg["name"])
        try:
            self.share.start()
        except OSError:
            self.share = None
        for e in [e for e in cfg["spaces"] if e["kind"] == "local"]:
            self.open_local(e)
        self.fix_current()

        self.tray = Tray(self)
        self.tray.command.connect(self.on_command)
        liveupdate.install(self)
        self.team_result.connect(self.on_team_result)
        self.bg_result.connect(self.on_bg_result)
        self.team_timer = QTimer(self, interval=TEAM_POLL_MS, timeout=self.poll_team)

    def save_later(self):
        self.save_timer.start()

    def on_screens_changed(self, *_):
        self.screens_changing = True
        self.screen_timer.start()

    def watch_screen(self, screen):
        screen.availableGeometryChanged.connect(self.on_screens_changed)

    def on_screens_settled(self):
        for ov in self.windows():
            if not ov.place_on_saved_screen():
                ov.keep_on_screen()
        self.screens_changing = False

    def win_cfg(self, mode):
        return self.cfg["windows"].setdefault(mode, {"pos": None, "panel": None, "open": False})

    def windows(self):
        return list(self.panels.values())

    def awake(self):
        """dogani 창을 눌러 쓰는 중인지. 아니면 창은 바탕화면처럼 굳어 있음 (호버 없음, 배경 옅게)"""
        return not self.cfg["wake_click"] or self.qapp.applicationState() == Qt.ApplicationState.ApplicationActive

    def push_awake(self, *_):
        for win in self.windows():   # 한 창을 눌러도 두 창이 함께 깨어남
            win.push_config(awake=self.awake())

    # ------------------------------------------------------------ spaces
    def entry(self, sid):
        return next((e for e in self.cfg["spaces"] if e.get("id") == sid), None) if sid else None

    def local_host(self, sid):
        return self.share.spaces.get(sid) if self.share and sid else None

    def joins(self):
        return [e for e in self.cfg["spaces"] if e["kind"] == "join"]

    def spaces(self):
        """창에 보내는 스페이스 목록 (내 PC 것 먼저). kind: me = 내 PC 폴더, team = 다른 PC 에 참여"""
        out = []
        for e in self.cfg["spaces"]:
            sid = e.get("id")
            if not sid:
                continue
            if e["kind"] == "local":
                host = self.local_host(sid)
                out.append({"key": sid, "name": host.name if host else (e.get("name") or os.path.basename(e["dir"])),
                            "server": f"127.0.0.1:{self.cfg['port']}", "prefix": f"/api/s/{sid}", "kind": "me",
                            "status": "online" if host else "offline", "sub": "" if host else "폴더 없음"})
            else:
                st = self.join_status.get(sid, "offline")
                out.append({"key": sid, "name": e.get("name") or "스페이스", "server": e["host"],
                            "prefix": f"/api/s/{sid}", "kind": "team",
                            "status": (self.status.get(sid) or "online") if st == "online" else
                            ("blocked" if st == "logged_out" else "offline")})
        return out

    def current_space(self):
        spaces = self.spaces()
        cur = next((s for s in spaces if s["key"] == self.cfg["space"]), None)
        return cur or (spaces[0] if spaces else
                       {"key": "", "name": "스페이스 없음", "server": f"127.0.0.1:{self.cfg['port']}",
                        "prefix": "/api/s/_", "kind": "me", "status": "offline", "sub": "스페이스 없음"})

    def fix_current(self):
        """보던 스페이스가 없어졌으면 첫 스페이스로"""
        if not any(s["key"] == self.cfg["space"] for s in self.spaces()):
            first = self.spaces()
            if first and self.cfg["space"] not in (ME, TEAM):   # 예전 키는 id 를 알 때까지 기다림
                self.cfg["space"] = first[0]["key"]

    def got_id(self, e):
        """스페이스 id 를 알게 됨 → 예전 키(me | team)로 기억한 보기·숨김을 id 로 옮김"""
        old = e.pop("legacy", None)
        if not old:
            return
        if self.cfg["space"] == old:
            self.cfg["space"] = e["id"]
        self.cfg["cal_hidden"] = [e["id"] if k == old else k for k in self.cfg["cal_hidden"]]
        self.save_later()

    def open_local(self, e, quiet=True):
        """내 PC 스페이스 폴더 열기 → TeamHost 또는 None (폴더가 없으면 목록에 남겨 두고 '폴더 없음')"""
        if not self.share:
            return None
        d = e["dir"]
        if not os.path.isdir(d):
            if not quiet:
                QMessageBox.warning(None, APP_NAME, f"폴더가 없습니다.\n{d}")
            return None
        new = not os.path.exists(os.path.join(space.meta_dir(d), "team.json"))
        try:
            host = TeamHost(d, TEAM_DIR, self.cfg["name"])
        except OSError as ex:
            if not quiet:
                QMessageBox.warning(None, APP_NAME, f"스페이스를 열지 못했습니다.\n{ex}")
            return None
        other = self.entry(host.space_id)
        if other is not None and other is not e:   # 폴더를 복사해 같은 id 가 둘
            if not quiet:
                QMessageBox.warning(None, APP_NAME, "이미 열려 있는 스페이스와 같은 스페이스입니다.\n"
                                    "폴더를 복사했다면 복사본의 .dogani 폴더를 지운 뒤 다시 열어 주세요.")
            return None
        if new:
            host.rename_team(e.get("name") or os.path.basename(os.path.normpath(d)))
        e["id"] = host.space_id
        e["name"] = host.name   # 폴더가 없을 때 보일 이름
        self.share.add_space(host)
        owner = next((m for m in host.members() if m["admin"]), None)
        if owner and owner["name"] != self.cfg["name"]:
            try:
                host.update_profile(owner["id"], self.cfg["name"])
            except TeamError:
                pass
        self.got_id(e)
        return host

    def pick_dir(self, title, own=None):
        """내 PC 스페이스로 쓸 폴더 고르기 → 경로 또는 "" (다른 내 스페이스와 겹치면 안 됨: 노트가 섞임). own = 폴더를 바꾸는 그 스페이스"""
        start = (own or {}).get("dir") or os.path.expanduser("~")
        while True:
            path = QFileDialog.getExistingDirectory(None, title, start)
            if not path:
                return ""
            path = os.path.normpath(path)
            clash = next((x["dir"] for x in self.cfg["spaces"] if x["kind"] == "local" and x is not own
                          and overlaps(path, x["dir"])), None)
            if not clash:
                return path
            QMessageBox.warning(None, APP_NAME, f"이미 연 스페이스와 겹치는 폴더입니다.\n{clash}\n\n다른 폴더를 골라 주세요.")
            start = path

    def create_space(self):
        path = self.pick_dir("스페이스로 쓸 폴더 선택  예) 폴더/2026.11/261102 월.md")
        if not path:
            return
        e = {"kind": "local", "dir": path}
        host = self.open_local(e, quiet=False)
        if not host:
            return
        self.cfg["spaces"].append(e)
        self.select_space(host.space_id)
        save_config(self.cfg)
        self.tray.update()

    def change_dir(self, sid):
        """폴더 바꾸기: 예전 폴더의 .dogani(id·팀원)를 새 폴더로 옮김 → 팀원 토큰 그대로. 노트는 옮기지 않음"""
        e = self.entry(sid)
        if not e or e["kind"] != "local":
            return
        old = e["dir"]
        path = self.pick_dir("스페이스 폴더 변경 (팀원은 그대로입니다)", e)
        if not path or os.path.normcase(path) == os.path.normcase(old):
            return
        had_meta = os.path.isdir(space.meta_dir(old)) and not os.path.exists(space.meta_dir(path))
        if os.path.isdir(old) and not self.carry_team_meta(old, path):
            return
        self.share.remove_space(sid)
        e["dir"] = path
        if not self.open_local(e, quiet=False):
            e["dir"] = old
            if had_meta and not os.path.exists(space.meta_dir(old)):   # 옮긴 .dogani 를 되돌림 (안 그러면 새 스페이스 = 팀원 모두 로그아웃)
                try:
                    shutil.move(space.meta_dir(path), space.meta_dir(old))
                except OSError:
                    pass
            self.open_local(e)
        self.fix_current()
        save_config(self.cfg)
        self.push_spaces()
        self.tray.update()

    def close_space(self, sid):
        e = self.entry(sid)
        if not e:
            return
        host = self.local_host(sid)
        msg = "이 스페이스를 목록에서 닫을까요?\n폴더와 노트는 그대로 남습니다."
        if host and not host.solo:
            msg += "\n닫는 동안 팀원은 이 스페이스를 볼 수 없습니다."
        if QMessageBox.question(None, APP_NAME, msg) != QMessageBox.StandardButton.Yes:
            return
        if self.share:
            self.share.remove_space(sid)
        self.cfg["spaces"].remove(e)
        self.fix_current()
        save_config(self.cfg)
        self.push_spaces()
        self.tray.update()

    def select_space(self, key):
        if key == self.cfg["space"] or not any(s["key"] == key for s in self.spaces()):
            return
        self.cfg["space"] = key
        self.save_later()
        self.push_spaces()
        self.tray.update()

    def on_status(self, key, status):
        if self.status.get(key) == status:
            return
        self.status[key] = status
        for win in self.windows():
            win.push_config(spaces=self.spaces())

    def js_common(self):
        # spaces = 달력에 같이 그릴 수 있는 스페이스 전부 (일정 팝업의 스페이스 선택에도 씀)
        return {"awake": self.awake(), "theme": self.cfg["theme"], "space": self.current_space(), "spaces": self.spaces(),
                "calHidden": self.cfg["cal_hidden"], "myName": self.cfg["name"]}

    def push_spaces(self):
        common = self.js_common()
        for win in self.panels.values():
            win.push_config(**common)

    def set_cal_hidden(self, keys):
        """달력·할일 창에서 숨길 스페이스 (창 하나에서 바꾸면 다른 창도 따라감)"""
        self.cfg["cal_hidden"] = [k for k in keys if isinstance(k, str)][:100]  # 스페이스 키 + 숨긴 태그("#태그", "#" = 태그 없음)
        self.save_later()
        for win in self.panels.values():
            win.push_config(calHidden=self.cfg["cal_hidden"])

    # ------------------------------------------------------------ 참여 스페이스 (다른 PC)
    def poll_team(self):
        for e in self.joins():
            self.poll_join(e)

    def poll_join(self, e):
        key = e.get("id") or e["token"]
        if key in self.polling:
            return
        self.polling.add(key)
        host, token, name, pc, pub, sid = e["host"], e["token"], self.cfg["name"], e.get("pc", ""), e.get("pub", ""), e.get("id")

        def work():
            status, res = 0, {}
            try:
                if not sid:   # 예전 설정: 토큰으로 스페이스 id 를 물음
                    status, res = http_json(host, "/api/me", token=token, name=name)
                    if status == 200 and res.get("space"):
                        found = res["space"]
                        status, res = http_json(host, f"/api/s/{found}/me", token=token, name=name)
                        res["_space"] = found
                    return
                status, res = http_json(host, f"/api/s/{sid}/me", token=token, name=name)
                if status == 0 and pc:
                    alt = find_host(pc, host, pub, sid)   # 서명으로 같은 호스트임을 확인한 주소에만 토큰을 보냄
                    if alt:
                        s2, r2 = http_json(alt, f"/api/s/{sid}/me", token=token, name=name)
                        if s2 == 200:   # 아니면 원래 결과(연결 안 됨) 그대로
                            status, res = 200, {**r2, "_host": alt}
            finally:   # 무슨 일이 있어도 결과를 보냄 (안 그러면 polling 이 안 풀려 다시 확인을 안 함)
                self.team_result.emit(status, {**res, "_polled": (key, host, token)})

        threading.Thread(target=work, daemon=True).start()

    def on_team_result(self, status, res):
        key, host, token = res["_polled"]
        self.polling.discard(key)
        e = next((x for x in self.joins() if (x.get("id") or x["token"]) == key), None)
        if not e:
            return
        if (e["host"], e["token"]) != (host, token):
            return self.poll_join(e)   # 확인하는 사이 주소를 바꿨거나 다시 참여함 → 지난 결과는 버리고 다시
        if not e.get("id"):
            if not res.get("_space"):
                if status in (401, 404):   # 내보내졌거나 호스트가 예전 버전: 목록에서 빼고 알림 (안 그러면 안 보이는 채로 계속 확인)
                    self.cfg["spaces"].remove(e)
                    self.save_later()
                    self.tray.notify("예전 팀 참여 정보로 다시 연결하지 못했습니다.\n초대장으로 다시 참여해 주세요.")
                return
            if self.entry(res["_space"]):   # 이미 목록에 있음 (다시 참여함)
                self.cfg["spaces"].remove(e)
                return self.save_later()
            e["id"] = res["_space"]
            self.got_id(e)
        self.apply_join_result(e, status, res)

    def apply_join_result(self, e, status, res):
        sid = e["id"]
        prev = self.join_status.get(sid)
        if status == 200:
            if res.get("_host"):   # PC 이름으로 바뀐 IP 를 찾음
                e["host"] = res["_host"]
                self.tray.notify(f"{e.get('name') or '스페이스'} 호스트의 주소가 바뀌어 {e['host']} 로 다시 연결했습니다.")
            e["pc"] = res.get("pc") or e.get("pc", "")
            self.join_status[sid] = "online"
            e["members"] = res.get("members") or []
            e["pub"] = res.get("pub") or e.get("pub", "")
            e["name"] = res.get("team") or e.get("name", "")
            me = res.get("me") or {}
            if me.get("name") and me["name"] != self.cfg["name"]:
                self.sync_name(e)
        elif status == 401:
            self.join_status[sid] = "logged_out"
            if prev != "logged_out":
                self.tray.notify(f"{e.get('name') or '스페이스'} 로그인이 풀렸습니다.\n"
                                 "내보내졌거나 다른 PC 로 다시 초대됐습니다. 주인에게 초대장을 받아 다시 참여해 주세요.")
        else:
            self.join_status[sid] = "offline"
        self.fix_current()
        self.save_later()
        self.push_spaces()

    def sync_name(self, e):
        """참여 스페이스의 내 이름을 '내 이름'에 맞춤 (꺼져 있던 동안 바꾼 이름도 다음 연결 때). 겹치면 한 번 알림"""
        key = ("name", e["id"])
        if key in self.polling:
            return
        self.polling.add(key)
        host, token, sid, name = e["host"], e["token"], e["id"], self.cfg["name"]

        def done(result):
            self.polling.discard(key)
            if result and result[0] == 409 and e.get("name_clash") != name:
                e["name_clash"] = name
                self.tray.notify(f"{e.get('name') or '스페이스'}: {result[1].get('message', '이름이 겹칩니다.')}")

        self.run_bg(lambda: http_json(host, f"/api/s/{sid}/profile", {"name": name}, token=token, name=name), done)

    @staticmethod
    def team_request_result(host, status, res):
        if status == 200:
            return True, res
        if status == 0:
            return False, f"{host} 에 연결하지 못했습니다.\n주소와 그 PC 의 dogani 가 켜져 있는지 확인해 주세요."
        if status == 404:
            return False, "그 PC 에 이 스페이스가 없습니다.\n주인에게 확인해 주세요."
        return False, res.get("message") or f"실패했습니다. (HTTP {status})"

    def join_space(self):
        last = next((e["host"] for e in reversed(self.joins())), "")
        dlg = TeamDialog(last, self.cfg["name"])
        joined = {}

        def handler(d):
            host = normalize_server(d.host.text())
            name = self.cfg["name"]
            while True:
                status, res = http_json(host, "/api/join", {"code": d.code.text(), "name": name})
                if status != 409 or res.get("error") != "name_taken":
                    break
                # 이름이 겹치면 그 자리에서 다른 이름을 물어 같은 초대장으로 다시 시도
                name, ok = QInputDialog.getText(d, "이름이 겹침", res.get("message", "") + "\n\n이 스페이스에서 쓸 다른 이름:",
                                                QLineEdit.EchoMode.Normal, name)
                name = clean_name(name) if ok else ""
                if not name:
                    return "취소했습니다.\n초대장은 15분 안에 다시 쓸 수 있습니다."
            ok, res = self.team_request_result(host, status, res)
            if not ok:
                return res
            sid = res.get("space")
            if not sid:
                return "그 PC 의 dogani 가 예전 버전입니다.\n업데이트한 뒤 다시 시도해 주세요."
            e = self.entry(sid)
            if self.local_host(sid) or (e is not None and e["kind"] != "join"):
                return "이 PC 의 스페이스입니다."
            if e is None:
                e = {"kind": "join", "id": sid}
                self.cfg["spaces"].append(e)
            e.update(host=host, pc=res.get("pc", ""), token=res["token"], mid=res["id"],
                     pub=res.get("pub", ""), name=res.get("team", ""))
            self.join_status[sid] = "online"
            joined["e"] = e
            return None

        dlg.handler = handler
        if dlg.exec() and joined:
            e = joined["e"]
            save_config(self.cfg)
            self.poll_join(e)
            self.select_space(e["id"])
            self.push_spaces()
            self.tray.update()
            self.tray.notify(f"{e.get('name') or '스페이스'}에 들어왔습니다.")

    def leave_space(self, sid):
        e = self.entry(sid)
        if not e or QMessageBox.question(None, APP_NAME, "이 스페이스에서 나갈까요?\n"
                                         "다시 들어오려면 주인의 초대장이 필요합니다.") != QMessageBox.StandardButton.Yes:
            return
        self.cfg["spaces"].remove(e)
        self.join_status.pop(sid, None)
        self.fix_current()
        save_config(self.cfg)
        self.push_spaces()
        self.tray.update()

    # ------------------------------------------------------------ 내 PC 스페이스
    def carry_team_meta(self, old, new):
        """스페이스 폴더를 바꿀 때 예전 폴더의 .dogani(스페이스 id·팀원 목록)를 새 폴더로 옮김
        → 같은 id = 같은 서명 키 = 팀원 토큰 그대로. 노트 파일은 옮기지 않음. 진행하면 True"""
        src, dst = space.meta_dir(old), space.meta_dir(new)
        if not os.path.isdir(src):
            return True
        if os.path.exists(dst):
            return QMessageBox.question(
                None, APP_NAME, f"새 폴더에 이미 다른 스페이스 정보(.dogani)가 있습니다.\n{new}\n\n"
                "이대로 열면 그 폴더의 스페이스가 되어, 지금 팀원은 모두 초대장으로 다시 참여해야 합니다.\n"
                "계속할까요?") == QMessageBox.StandardButton.Yes
        try:
            shutil.move(src, dst)
        except OSError as e:
            QMessageBox.warning(None, APP_NAME, f"스페이스 정보(.dogani)를 새 폴더로 옮기지 못했습니다.\n{e}")
            return False
        return True

    def rename(self):
        name, ok = QInputDialog.getText(None, "내 이름", "팀원에게 보이는 이름 (최대 20자)",
                                        QLineEdit.EchoMode.Normal, self.cfg["name"])
        name = clean_name(name) if ok else ""
        if not name or name == self.cfg["name"]:
            return
        for host in (self.share.spaces.values() if self.share else ()):
            try:
                host.update_profile(host.owner_id, name)
            except TeamError as ex:
                QMessageBox.warning(None, APP_NAME, f"{host.name}: {ex.message}")
                return
        self.cfg["name"] = name
        if self.share:
            self.share.name = name
        self.poll_team()   # 참여 스페이스 이름은 연결 확인 결과에서 맞춤 (sync_name)
        self.push_spaces()
        self.tray.update()

    def rename_space(self, sid):
        host = self.local_host(sid)
        if not host:
            return
        text, ok = QInputDialog.getText(None, "스페이스 이름", "이름", QLineEdit.EchoMode.Normal, host.name)
        if not ok or not clean_name(text):
            return
        host.rename_team(text)
        self.entry(sid)["name"] = host.name
        host.hub.broadcast("schedule", {})   # 팀원 화면도 /me 로 새 이름을 받음
        self.save_later()
        self.push_spaces()
        self.tray.update()

    def change_host(self, sid):
        """호스트 IP 가 바뀌었을 때 주소만 바꿈 (토큰 그대로). 같은 호스트(서명 확인)일 때만 바꿈"""
        e = self.entry(sid)
        if not e or e["kind"] != "join":
            return
        text, ok = QInputDialog.getText(None, "호스트 주소", "호스트 PC 의 새 주소 (예: 192.168.0.12:8765)",
                                        QLineEdit.EchoMode.Normal, e["host"])
        host = normalize_server(text) if ok else ""
        if not host or host == e["host"]:
            return
        token, pub, name = e["token"], e.get("pub", ""), self.cfg["name"]

        def work():   # 주소가 틀리면 몇 초 걸리므로 작업 스레드에서 (트레이·창이 멈추지 않게)
            proven, status, res = host_proof(host, pub, sid)
            if status != 200:
                return status, res, False
            if not proven:   # 서명이 안 맞음 = 다른 스페이스. 토큰은 안 보냄
                return status, res, True
            return *http_json(host, f"/api/s/{sid}/me", token=token, name=name), False

        def done(result):
            if self.entry(sid) is not e or e["token"] != token:   # 그 사이 나갔거나 다시 참여함
                return
            if result is None:
                return QMessageBox.warning(None, APP_NAME, "호스트를 확인하지 못했습니다.\n다시 시도해 주세요.")
            status, res, other = result
            if other:
                return QMessageBox.warning(None, APP_NAME, "그 주소는 이 스페이스의 호스트로 확인되지 않습니다.\n"
                                           "다른 PC 이거나, 그 PC 의 dogani 가 예전 버전입니다.")
            if status == 401:
                return QMessageBox.warning(None, APP_NAME, "그 호스트가 이 PC 를 팀원으로 인정하지 않습니다.\n"
                                           "초대장으로 다시 참여해 주세요.")
            if status != 200:
                return QMessageBox.warning(None, APP_NAME, self.team_request_result(host, status, res)[1])
            e["host"] = host
            save_config(self.cfg)
            # polling 은 건드리지 않음: 옛 주소로 확인 중이던 결과는 on_team_result 가 버리고 새 주소로 다시 확인
            self.apply_join_result(e, status, res)
            self.tray.notify(f"호스트 주소를 {host} 로 바꿨습니다.")

        self.run_bg(work, done)

    def run_bg(self, work, done):
        """work() 는 작업 스레드에서, 그 결과로 done(결과) 는 Qt 메인 스레드에서 실행. work 가 예외면 done(None)"""
        def run():
            try:
                result = work()
            except Exception:
                result = None
            self.bg_result.emit((done, result))

        threading.Thread(target=run, daemon=True).start()

    def on_bg_result(self, payload):
        done, result = payload
        done(result)

    # ------------------------------------------------------------ windows
    def show_panel(self, mode):
        self.win_cfg(mode)["open"] = True
        win = self.panels.get(mode)
        if not win:
            win = Overlay(self, mode)
            self.panels[mode] = win
        win.reveal()
        self.save_later()
        return win

    def close_panel(self, mode, forget=True):
        win = self.panels.pop(mode, None)
        if forget:
            self.win_cfg(mode)["open"] = False
        if win:
            win.close()
            win.deleteLater()
        self.save_later()

    # 트레이 좌클릭 순서. 지금 열린 창 조합의 다음 것으로 바꿈 (네 조합을 모두 담아 어느 상태에서든 이어짐)
    PANEL_CYCLE = (("cal",), ("note",), ("note", "cal"), ())

    def cycle_panels(self):
        now = tuple(m for m in ("note", "cal") if m in self.panels)
        order = self.PANEL_CYCLE   # 조합 안 순서를 now 와 같게(note, cal) 적어 둠
        nxt = order[(order.index(now) + 1) % len(order)]
        for mode in ("note", "cal"):
            if mode in nxt and mode not in self.panels:
                self.show_panel(mode)
            elif mode not in nxt and mode in self.panels:
                self.close_panel(mode)

    def toggle_panel(self, mode):
        if mode in self.panels:
            self.close_panel(mode)
        else:
            self.show_panel(mode)

    def open_note(self, date):
        """달력에서 날짜 클릭 → 할일 창을 열고(이미 열려 있으면 그대로) 그 날짜로"""
        win = self.panels.get("note")
        if win:
            win.show()
            win.raise_()
            win.call("showDate", date)
        else:
            self.win_cfg("note")["date"] = date
            self.show_panel("note")

    def open_task(self, date, line, raw):
        """달력에서 할일 클릭 → 할일 창에서 그 노트의 그 줄을 강조 (창이 닫혀 있으면 그 날짜로 열기만)"""
        win = self.panels.get("note")
        if not win:
            return self.open_note(date)
        win.show()
        win.raise_()
        win.call("showTask", date, line, raw)

    def note_date_changed(self, date, title, is_today):
        cal = self.panels.get("cal")
        if cal:
            cal.push_config(noteDate=date)

    def start(self):
        self.tray.update()
        self.tray.start()
        fresh = not any(m in self.cfg["windows"] for m in ("note", "cal"))   # 처음 실행: 할일 창 하나 열어 둠
        for mode in ("note", "cal"):
            if self.win_cfg(mode).get("open") or (fresh and mode == "note"):
                self.show_panel(mode)
        self.team_timer.start()
        self.poll_team()
        if not self.share:
            self.tray.notify(f"포트 {self.cfg['port']}이(가) 사용 중이라 스페이스를 열 수 없습니다.\n"
                             "예전 버전 dogani 가 켜져 있다면 종료하고 다시 실행해 주세요.")
        elif not self.cfg["spaces"]:
            QTimer.singleShot(500, self.create_space)

    # ------------------------------------------------------------ actions
    def invite_for(self, name, keys):
        """name 님용 새 초대장: 내 PC 스페이스 중 고름 (keys 에 든 것 먼저). 하나뿐이면 묻지 않음"""
        hosts = [(s["key"], s["name"]) for s in self.spaces() if s["kind"] == "me" and self.local_host(s["key"])]
        hosts.sort(key=lambda h: h[0] not in keys)
        if not hosts:
            return self.tray.notify("초대장을 만들 수 있는 내 PC 스페이스가 없습니다.")
        if len(hosts) == 1:
            if QMessageBox.question(None, APP_NAME, f"{name} 님을 '{hosts[0][1]}' 스페이스에 초대할까요?\n"
                                    "초대장을 만들어 복사합니다.") != QMessageBox.StandardButton.Yes:
                return
            sid = hosts[0][0]
        else:
            label, ok = QInputDialog.getItem(None, APP_NAME, f"{name} 님을 초대할 스페이스를 골라 주세요.",
                                             [h[1] for h in hosts], 0, False)
            if not ok:
                return
            sid = next(h[0] for h in hosts if h[1] == label)
        self.space_command("new_invite", self.local_host(sid), [])

    def space_command(self, name, host, rest):
        """내 PC 스페이스의 초대장·팀원 명령 (트레이)"""
        if name in ("new_invite", "reinvite", "copy_invite"):
            try:
                code = rest[0] if name == "copy_invite" else host.create_invite(rest[0] if name == "reinvite" else None)
            except TeamError as e:
                return self.tray.notify(e.message)
            QGuiApplication.clipboard().setText(f"dogani 스페이스 참여\n호스트 주소: {self.share.address}\n초대장 코드: {code}")
            self.tray.notify(f"초대장 {code} 를 복사했습니다.\n15분 안에 한 번만 쓸 수 있습니다. 메신저로 그 사람에게 보내 주세요.")
        elif name == "cancel_invite":
            host.cancel_invite(rest[0])
        elif name == "ask_cancel_invite":
            if QMessageBox.question(None, APP_NAME, f"초대장 {rest[0]} 을(를) 삭제할까요?") == QMessageBox.StandardButton.Yes:
                host.cancel_invite(rest[0])
        elif name == "kick":
            m = next((m for m in host.members() if m["id"] == rest[0]), None)
            if m and QMessageBox.question(None, APP_NAME, f"{m['name']} 님을 이 스페이스에서 내보낼까요?") == QMessageBox.StandardButton.Yes:
                host.kick(rest[0])
        elif name == "perm":
            try:
                host.set_perm(*rest)
            except TeamError as e:
                self.tray.notify(e.message)

    def on_command(self, name, arg):
        cfg = self.cfg
        share = self.share
        if name == "toggle_panel":
            self.toggle_panel(arg)
        elif name == "cycle_panels":
            self.cycle_panels()
        elif name == "select_space":
            self.select_space(arg)
        elif name == "rename":
            self.rename()
        elif name == "create_space":
            self.create_space()
        elif name == "join_space":
            self.join_space()
        elif name == "leave_space":
            self.leave_space(arg)
        elif name == "change_host":
            self.change_host(arg)
        elif name == "close_space":
            self.close_space(arg)
        elif name == "change_dir":
            self.change_dir(arg)
        elif name == "rename_space":
            self.rename_space(arg)
        elif name == "open_dir":
            e = self.entry(arg)
            if e and e["kind"] == "local" and os.path.isdir(e["dir"]):
                os.startfile(e["dir"])
        elif name in ("new_invite", "reinvite", "copy_invite", "cancel_invite", "ask_cancel_invite", "kick", "perm"):
            sid, *rest = arg if isinstance(arg, tuple) else (arg,)
            host = self.local_host(sid)
            if host:
                self.space_command(name, host, rest)
        elif name == "toggle_wake":
            cfg["wake_click"] = not cfg["wake_click"]
            self.push_awake()
        elif name == "toggle_theme":
            cfg["theme"] = "light" if cfg["theme"] == "dark" else "dark"
            for ov in self.windows():
                ov.push_config(theme=cfg["theme"])
        elif name == "toggle_autostart":
            set_autostart(not is_autostart())
        elif name == "quit":
            save_config(cfg)
            if share:
                share.stop()
            self.tray.stop()
            self.qapp.quit()
            return
        save_config(cfg)


def single_instance(name="DoganiMutex"):
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main():
    parser = argparse.ArgumentParser(description="dogani - 스페이스 할일·일정 오버레이 + 팀스페이스")
    parser.add_argument("--dir", help="열 스페이스(노트 폴더) 경로")
    parser.add_argument("--port", type=int, help=f"포트 (기본 {DEFAULT_PORT}, 팀 호스트 주소에 씀)")
    parser.add_argument("--name", help="내 이름")
    args = parser.parse_args()

    register_ui_scheme()
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    mutex = liveupdate.MUTEX = single_instance()
    if mutex is None:
        QMessageBox.information(None, APP_NAME, "dogani 가 이미 실행 중입니다.\n시스템 트레이를 확인하세요.")
        return

    cfg = load_config()
    if args.dir:
        d = os.path.normpath(os.path.abspath(args.dir))
        if not any(e.get("dir") == d for e in cfg["spaces"]):
            cfg["spaces"].append({"kind": "local", "dir": d})
    if args.port:
        cfg["port"] = args.port
    if args.name and clean_name(args.name):
        cfg["name"] = clean_name(args.name)
    save_config(cfg)

    app = doganiApp(qapp, cfg)
    app.start()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
