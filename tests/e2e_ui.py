"""두 화면(A, B) + 실제 서버로 끊김·동시편집 시험 (화면은 offscreen, 설정은 임시 폴더라 실제 설정·노트를 안 건드림).
실행 (저장소 루트): "C:\Program Files\Python313\python.exe" tests/e2e_ui.py   — 약 2분 걸림, 마지막 줄 "N/N 통과"
unittest discover 에는 안 걸림 (QtWebEngine 이 필요해 따로 돌림)"""
import faulthandler; faulthandler.dump_traceback_later(240, exit=True)
import os, sys, json, time, tempfile, datetime as dt, glob
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
TMP = tempfile.mkdtemp(prefix="dogani-e2e-")
os.environ["DOGANI_HOME"] = os.path.join(TMP, "home")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, APP)
os.chdir(APP)

from PySide6.QtCore import QObject, QTimer, QUrl, QEventLoop
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
import dogani, server, team

qapp = QApplication(sys.argv)


class Fake(QObject):
    cfg = {"name": "tester", "spaces": []}


fake = Fake()
dogani.setup_profile(fake)

SPACE = os.path.join(TMP, "space")
KEYS = os.path.join(TMP, "keys")
os.makedirs(SPACE); os.makedirs(KEYS)
PORT = 18765
srv = None
host = team.TeamHost(SPACE, KEYS, "t")


def start_server():
    global srv
    srv = server.ShareServer(PORT, "t")
    srv.add_space(host)
    for _ in range(50):
        try:
            srv.start(); return
        except OSError:
            wait(0.2)


def stop_server():
    srv.stop()
    try:
        srv.httpd.server_close()
    except Exception:
        pass
    host.hub.__init__()   # SSE 연결 정리 시도


def wait(sec):
    loop = QEventLoop(); QTimer.singleShot(int(sec * 1000), loop.quit); loop.exec()


CONFIRM = {"answer": False, "asked": 0}


class TPage(QWebEnginePage):
    def __init__(self, name):
        super().__init__()
        self.name = name

    def javaScriptConfirm(self, url, msg):
        CONFIRM["asked"] += 1
        print(f"  [{self.name}] confirm → {CONFIRM['answer']}")
        return CONFIRM["answer"]

    def javaScriptAlert(self, url, msg):
        print(f"  [{self.name}] alert: {msg}")

    def javaScriptConsoleMessage(self, level, msg, line, src):
        if level >= QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel:
            print(f"  [{self.name}] console: {msg} ({line})")


def js(view, code):
    t0 = time.time()
    out = {}
    loop = QEventLoop()
    view.page().runJavaScript(code, 0, lambda r: (out.setdefault("r", r), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    if time.time() - t0 > 4: print("  js slow", code[:60], flush=True)
    return out.get("r")


def open_view(name):
    v = QWebEngineView(); v.setPage(TPage(name)); v.resize(500, 700)
    v.load(QUrl(f"dogani://ui/index.html?mode=note&server=127.0.0.1:{PORT}&prefix=/api/s/{host.space_id}"))
    v.loadFinished.connect(lambda ok, v=v: v.page().runJavaScript(
        "window.__asked = 0; window.confirm = (m) => { window.__asked++; return !!window.__answer; }; window.alert = (m) => console.warn('alert ' + m);"))
    v.show()
    return v


def doc(v):
    return js(v, "(() => { const n = window.__dogani.note; return n.lines ? n.lines.join('\\n') : null; })()")


def add(v, n, text):
    """n 번째 줄(0부터) 끝에 text 를 침 (사람이 친 것처럼)"""
    return js(v, f"""(() => {{ const n = window.__dogani.note; if (!n.editor) return 'noeditor';
      let pos = 0; for (let i = 0; i < {n}; i++) pos += n.lines[i].length + 1; pos += n.lines[{n}].length;
      n.editor.focus(pos); if (!n.editor.hasFocus() && document.querySelector('.cm-content').contentEditable === 'true') n.editing = true; document.execCommand('insertText', false, {json.dumps(text)}); return 'ok'; }})()""")


def readonly(v):
    return js(v, "(() => { const c = document.querySelector('.cm-content'); return c ? c.contentEditable === 'false' : null; })()")


def blur(v):
    js(v, "window.__dogani.note.exit()")


def footer(v):
    return js(v, "document.getElementById('note-footer').textContent")


today = dt.date.today()
WD = "월화수목금토일"[today.weekday()]
folder = os.path.join(SPACE, today.strftime("%Y.%m"))
os.makedirs(folder)
NOTE = os.path.join(folder, f"{today:%y%m%d} {WD}.md")


def write_file(t):
    with open(NOTE, "w", encoding="utf-8", newline="") as f:
        f.write(t)
    st = os.stat(NOTE); os.utime(NOTE, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))


def read_file():
    with open(NOTE, encoding="utf-8", newline="") as f:
        return f.read()


results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  ← " + detail))


def reload_views(*vs):
    for v in vs:
        v.reload()
    wait(2.5)


write_file("- [ ] a\n- [ ] b\n- [ ] c\n")
start_server()
A = open_view("A"); B = open_view("B")
wait(3)
check("두 화면에 노트 뜸", doc(A) == doc(B) == "- [ ] a\n- [ ] b\n- [ ] c\n", repr((doc(A), doc(B))))

# 1. 동시 편집, 다른 줄 (거의 같은 순간)
print("add", add(A, 0, " A1"), flush=True); print("add", add(B, 1, " B1"), flush=True)
for _ in range(6):
    wait(0.5); print("  t", repr(read_file()), "|A", footer(A), "|B", footer(B), "|edA", js(A,"window.__dogani.note.editing"), "|edB", js(B,"window.__dogani.note.editing"), flush=True)
check("1 다른 줄 동시 편집 → 둘 다 반영", read_file() == "- [ ] a A1\n- [ ] b B1\n- [ ] c\n", repr(read_file()))
blur(A); blur(B); wait(2)
check("1 두 화면 모두 최신으로", doc(A) == doc(B) == read_file(), repr((doc(A), doc(B))))
blur(A); blur(B); wait(1)


# 2. 같은 줄 다르게 → 물어봄. 취소 = 상대 것
js(A, "window.__answer = false; window.__asked = 0"); js(B, "window.__answer = false; window.__asked = 0")
print("before", repr(doc(A)), repr(doc(B)), repr(read_file()))
add(A, 2, "-A"); add(B, 2, "-B")
for _ in range(6):
    wait(0.5); print("  T", repr(read_file()), "|A", repr(doc(A)), footer(A), "|B", repr(doc(B)), footer(B), flush=True)
f = read_file()
check("2 같은 줄 충돌 시 한 번 물어봄", (js(A, "window.__asked") + js(B, "window.__asked")) == 1, str((js(A, "window.__asked"), js(B, "window.__asked"))))
check("2 파일엔 먼저 저장한 쪽 내용", f.split("\n")[2] in ("- [ ] c-A", "- [ ] c-B"), repr(f))
blur(A); blur(B); wait(2)
check("2 취소한 쪽도 파일 내용으로 맞춰짐", doc(A) == doc(B) == f, repr((doc(A), doc(B), f)))

# 3. 끊긴 동안 쓴 내용 (A 가 쓰자마자 호스트 꺼짐), 그 사이 파일은 다른 곳에서 다른 줄 수정
write_file("- [ ] a\n- [ ] b\n- [ ] c\n"); wait(2.5)
reload_views(A, B)
stop_server(); wait(0.5)
add(A, 0, " (집에서)"); wait(2); blur(A); wait(1)
fa = footer(A)
check("3 끊김: 보관 안내", "보관" in (fa or ""), repr(fa))
ro = readonly(A)
check("3 끊긴 동안 읽기 전용", ro is True, repr(ro))
write_file("- [ ] a\n- [ ] b\n- [x] c (사무실에서)\n")
start_server(); wait(8)   # SSE 재연결 (브라우저 재시도)
f = read_file()
check("3 다시 연결 → 합쳐 저장", f == "- [ ] a (집에서)\n- [ ] b\n- [x] c (사무실에서)\n", repr(f))
check("3 A 화면 = 파일", doc(A) == f, repr(doc(A)))
ro = readonly(A)
check("3 다시 쓸 수 있음", ro is False, repr(ro))

# 4. 끊긴 동안 화면을 다시 열어도(재시작) 보관 내용이 보임 — 브라우저 모드엔 브리지가 없어 메모리만 → 건너뜀(트레이 앱은 unsaved.json)
# 4'. unsaved.json 저장 함수
dogani.save_unsaved("me|2026-11-18", json.dumps({"content": "x", "from": None, "at": 1}))
check("4 unsaved.json 저장·읽기", dogani.load_unsaved().get("me|2026-11-18", {}).get("content") == "x")
dogani.save_unsaved("me|2026-11-18", "")
check("4 unsaved.json 지우기", "me|2026-11-18" not in dogani.load_unsaved())

# 5. 새 노트 둘이 동시에 만들기 (빈 날짜) → 둘 다 남음
os.remove(NOTE); wait(2.5)
reload_views(A, B)
for v in (A, B):
    js(v, "document.querySelector('#note-body .empty').click()")
wait(0.5)
print("5 start", repr(doc(A)), repr(doc(B)), os.path.exists(NOTE))
add(A, 0, "- [ ] A 할일"); add(B, 0, "- [ ] B 할일")
for _ in range(6):
    wait(0.5); print("  T", repr(read_file() if os.path.exists(NOTE) else None), "|A", repr(doc(A)), footer(A), "|B", repr(doc(B)), footer(B), flush=True); blur(A); blur(B); wait(2)
f = read_file() if os.path.exists(NOTE) else None
check("5 동시에 새 노트 → 둘 다 남음", f is not None and "A 할일" in f and "B 할일" in f, repr(f))

# 6. 서버: base_mtime null = 새 파일일 때만
import urllib.request, urllib.error
def post(path, body):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/s/{host.space_id}{path}", data=json.dumps(body).encode(), method="POST",
                               headers={server.KEY_HEADER: server.CLIENT_KEY, "Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(r).status
    except urllib.error.HTTPError as e:
        return e.code
check("6 노트 null + 파일 있음 → 409", post("/note", {"date": today.isoformat(), "content": "z", "base_mtime": None}) == 409)
check("6 일정 null + 파일 있음 → 409", (post("/sched-doc", {"content": "", "base_mtime": None}) if True else 0) in (200, 409))
check("6 base_mtime 키 없음(예전 방식) → 덮어씀", post("/note", {"date": today.isoformat(), "content": "z\n"}) == 200)

stop_server()
ok = sum(1 for _, o in results if o)
print(f"\n{ok}/{len(results)} 통과")
qapp.quit()
sys.stdout.flush()
os._exit(0 if ok == len(results) else 1)
