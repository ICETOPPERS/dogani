"""
노트 서버 (dogani.py 안에서 스레드로 실행)

- 스페이스 폴더(데일리 노트 YYMMDD 요일.md + 일정 파일)를 감시하다가 변경되면 SSE 로 창에 푸시
- 스페이스 /api/s/<스페이스id>/* : 이 PC 에서 연 스페이스(폴더)마다 하나. 같은 PC(나)는 항상 주인,
  다른 PC 는 그 스페이스의 팀원 토큰이 있어야 하고 쓰기는 권한이 write 인 팀원만 (team.can_write: 개인 지정 > 그룹).
  팀원이 없는 스페이스는 다른 PC 에 열리지 않음 (= 예전 개인 스페이스)
- /api/join : 초대장 코드로 참여 (코드가 어느 스페이스 것인지 서버가 찾음), /api/me : 토큰으로 스페이스 찾기
"""
import datetime as dt
import hmac
import json
import os
import queue
import re
import secrets
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import schedule

from team import FAIL_LIMIT, FAIL_LOCK, INVITE_TTL, TeamError, bad_code

POLL_INTERVAL = 1.0      # 파일 변경 감시 주기(초)
HEARTBEAT_INTERVAL = 15  # SSE keep-alive 주기(초)

# 파일명 규칙: "261030 금.md" (YYMMDD) 또는 "2026-10-30.md" (ISO 날짜 이름)
FILE_PATTERNS = [
    (re.compile(r"^(\d{2})(\d{2})(\d{2})(?:[\s_].*)?\.md$", re.I), lambda m: (2000 + int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[\s_].*)?\.md$", re.I), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
]
# dogani 프로그램 식별용 헤더 (보안 인증이 아니라 "브라우저로 그냥 열람"을 막는 용도)
KEY_HEADER = "X-dogani-Key"


def _web_origin(headers):
    """오버레이(dogani:// 스킴) 가 아닌 웹페이지에서 온 요청인지"""
    return (headers.get("Origin") or "").lower().startswith(("http:", "https:"))


HOST_HEADER = "X-dogani-Host"
TOKEN_HEADER = "X-dogani-Token"   # 팀원 토큰 (team.py)
USER_HEADER = "X-dogani-User"     # 표시 이름 (URL 인코딩)
# 주인(이 PC 의 dogani) 비밀값: 켤 때마다 새로 만들어 메모리에만 둠. 127.0.0.1 로 가는 요청에만 붙임.
# IP 로 주인을 가리면 IP 가 바뀐 뒤 그 IP 를 받은 PC·같은 PC 의 다른 사용자가 관리자가 되므로 비밀값으로 가림
OWNER_HEADER = "X-dogani-Owner"
CLIENT_KEY = "dogani-client-v1"
TASK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[(.)\]")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
WEEKDAYS = "월화수목금토일"
MAX_NOTE_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------- notes
def parse_note_date(filename):
    for pattern, conv in FILE_PATTERNS:
        m = pattern.match(filename)
        if m:
            try:
                return dt.date(*conv(m))
            except ValueError:
                return None
    return None


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("utf-8-sig", errors="replace")


def atomic_write(path, content):
    tmp = path + ".dogani.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp, path)


def count_tasks(text):
    """체크박스 항목 수(하위 항목 포함). 공백이 아닌 상태 문자는 모두 완료로 본다(`[/]`·`[-]` 같은 확장 표기 포함)."""
    done = total = 0
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = TASK_RE.match(line)
        if m:
            total += 1
            if m[1] != " ":
                done += 1
    return done, total


TASK_DATE_RE = re.compile("\\s*(\U0001F6EB|\U0001F4C5)\uFE0F?\\s*(\\d{4}-\\d{2}-\\d{2})")   # 🛫 시작 / 📅 마감 (이모지 날짜 형식)
TASK_DONE_RE = re.compile("\\s*\u2705\uFE0F?\\s*(\\d{4}-\\d{2}-\\d{2})")   # ✅ 완료 날짜 (이모지 날짜 형식)
TASK_HEAD_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[.\]\s*")
TASK_MARK_RE = re.compile(r"\*\*|==|~~|\[\[|\]\]")


def dated_tasks(text):
    """날짜(🛫/📅)가 붙은 체크박스 줄 → [{line, raw, done, title, start, end}] (달력에 비춰 보이는 할일)"""
    out, in_fence = [], False
    for i, line in enumerate(text.split("\n")):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        m = None if in_fence else TASK_RE.match(line)
        if not m:
            continue
        dates = {k: v for k, v in TASK_DATE_RE.findall(line)}
        if not dates:
            continue
        try:
            for v in dates.values():
                dt.date.fromisoformat(v)
        except ValueError:
            continue
        start = dates.get("\U0001F6EB") or dates["\U0001F4C5"]
        end = dates.get("\U0001F4C5") or start
        if end < start:
            start, end = end, start
        # ✅ 완료 날짜: 체크된 줄만, 틀린 날짜는 없는 셈 (달력은 이 날에 그림)
        done_on = TASK_DONE_RE.search(line) if m[1] != " " else None
        try:
            done_on = done_on and dt.date.fromisoformat(done_on[1]).isoformat()
        except ValueError:
            done_on = None
        title = TASK_MARK_RE.sub("", TASK_DONE_RE.sub("", TASK_DATE_RE.sub("", TASK_HEAD_RE.sub("", line)))).strip()
        out.append({"line": i, "raw": line.rstrip("\r"), "done": m[1] != " ", "done_on": done_on or None,
                    "title": title or "(제목 없음)", "start": start, "end": end})
    return out


class NoteStore:
    """todo 폴더를 주기적으로 스캔해서 날짜→파일 매핑과 완료 통계를 유지."""

    def __init__(self):
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()   # 저장(mtime 확인 + 쓰기)을 하나씩
        self.root = ""
        self.files = {}    # date -> (path, mtime_ns, size)
        self.summary = {}  # date -> (done, total)
        self.tasks = {}    # date -> 날짜 붙은 할일 (dated_tasks)
        self.blank = set()       # 내용이 빈 노트 날짜 (어제 노트 복사 대상에서 뺌)
        self.sched_files = {}    # path -> (mtime_ns, size)
        self.events = []         # 일정 파일에서 읽은 일정
        self.decos = []          # 일정 파일의 장식 줄 (일정과 따로: 예전 버전 클라이언트가 빈 일정으로 그리지 않게)
        self.sched_changed = False
        self.pending = set()     # 아직 알리지 않은 바뀐 날짜 (publish 가 가져감)

    def set_root(self, root):
        with self.lock:
            self.root = root
            self.files = {}
            self.summary = {}
            self.tasks = {}
            self.blank = set()
            self.sched_files = {}
            self.events = []
            self.decos = []

    def has_root(self):
        with self.lock:
            return bool(self.root) and os.path.isdir(self.root)

    def _scan(self, root):
        """1초마다 도는 폴더 스캔. Windows 는 scandir 항목에 stat 이 같이 와서 파일마다 시스템 호출이 없다"""
        found, sched = {}, {}
        if not root or not os.path.isdir(root):
            return found, sched
        stack = [root]
        while stack:
            try:
                with os.scandir(stack.pop()) as it:
                    entries = list(it)
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir():
                        if not e.name.startswith("."):
                            stack.append(e.path)
                        continue
                    if schedule.is_schedule_file(e.name):
                        st = e.stat()
                        sched[e.path] = (st.st_mtime_ns, st.st_size)
                        continue
                    d = parse_note_date(e.name)
                    if d is None:
                        continue
                    st = e.stat()
                except OSError:
                    continue
                # 같은 날짜 파일이 여러 개면 최근 수정된 것을 사용
                if d not in found or st.st_mtime_ns > found[d][1]:
                    found[d] = (e.path, st.st_mtime_ns, st.st_size)
        return found, sched

    def refresh(self):
        """폴더를 다시 읽어 바뀐 날짜를 pending 에 쌓는다 (알림은 publish)."""
        with self.lock:
            root = self.root
        found, sched = self._scan(root)
        events = None
        if sched != self.sched_files:
            events = []
            for path in sorted(sched):
                try:
                    events += schedule.parse(read_text(path))
                except OSError:
                    pass
            events.sort(key=schedule.sort_key)
        changed = []
        with self.lock:
            if root != self.root:  # 스캔 도중 경로가 바뀜
                return
            for d, info in found.items():
                if self.files.get(d) != info:
                    changed.append(d)
                    try:
                        text = read_text(info[0])
                        self.summary[d] = count_tasks(text)
                        self.tasks[d] = dated_tasks(text)
                        self.blank.discard(d) if text.strip() else self.blank.add(d)
                    except OSError:
                        self.summary[d] = (0, 0)
                        self.tasks.pop(d, None)
            for d in list(self.files):
                if d not in found:
                    changed.append(d)
                    self.summary.pop(d, None)
                    self.tasks.pop(d, None)
                    self.blank.discard(d)
            self.files = found
            self.pending.update(changed)
            if events is not None:
                self.sched_files = sched
                self.events = [e for e in events if not e.get("deco")]
                self.decos = [e for e in events if e.get("deco")]
                self.sched_changed = True

    def pop_changes(self):
        """-> (바뀐 날짜 iso 목록, 일정 바뀜 여부). 가져가면 비움"""
        with self.lock:
            dates = sorted(d.isoformat() for d in self.pending)
            sched, self.pending, self.sched_changed = self.sched_changed, set(), False
        return dates, sched

    def schedule(self):
        with self.lock:
            return list(self.events)

    def decorations(self):
        with self.lock:
            return list(self.decos)

    def dated_tasks(self):
        """모든 노트의 날짜 붙은 할일 (note = 노트 날짜)"""
        with self.lock:
            return [{**t, "note": d.isoformat()} for d in sorted(self.tasks) for t in self.tasks[d]]

    def prev_note(self, d):
        """d 이전에 있는 가장 최근 노트(빈 노트 제외) 날짜 (어제 노트 복사용)"""
        with self.lock:
            earlier = [x for x in self.files if x < d and x not in self.blank]
        return max(earlier).isoformat() if earlier else None

    def note(self, d):
        with self.lock:
            info = self.files.get(d)
        if not info:
            return {"date": d.isoformat(), "exists": False, "no_dir": not self.has_root(), "prev": self.prev_note(d)}
        path, mtime_ns, _ = info
        try:
            text = read_text(path)
        except OSError:
            return {"date": d.isoformat(), "exists": False}
        done, total = count_tasks(text)
        return {
            "date": d.isoformat(),
            "exists": True,
            "title": os.path.splitext(os.path.basename(path))[0],
            "mtime": mtime_ns // 1_000_000,
            "content": text,
            "done": done,
            "total": total,
            "prev": self.prev_note(d) if not text.strip() else None,
        }

    def write(self, d, content, base_mtime=None, new_only=False):
        """노트 저장. 파일이 없으면 선택폴더/yyyy.mm/YYMMDD 요일.md 로 새로 만든다.
        base_mtime 이 현재 파일과 다르면(그 사이 다른 팀원·다른 프로그램이 고침) 저장하지 않고 None.
        new_only = 없는 파일로 알고 씀(base_mtime: null) → 그 사이 누가 만들었으면 None"""
        with self.write_lock:   # 확인~쓰기 사이에 다른 저장이 끼면 둘 다 통과해 앞의 것이 사라짐
            with self.lock:
                info = self.files.get(d)
                root = self.root
            if info:
                path = info[0]
                if new_only or (base_mtime is not None and os.stat(path).st_mtime_ns // 1_000_000 != base_mtime):
                    return None
            else:
                if not root or not os.path.isdir(root):
                    raise OSError("스페이스 폴더가 지정되지 않았습니다.")
                folder = os.path.join(root, f"{d.year:04d}.{d.month:02d}")
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, f"{d:%y%m%d} {WEEKDAYS[d.weekday()]}.md")
                if new_only and os.path.exists(path):   # 목록엔 아직 없지만(다른 프로그램이 방금 만듦 등) 파일이 있음
                    self.refresh()   # 다시 받을 때 그 파일이 보이게
                    return None
            atomic_write(path, content)
            self.refresh()
            return self.note(d)

    def sched_doc_path(self):
        """편집할 일정 파일: 있으면 첫 번째(올해 것 우선), 없으면 선택폴더/일정.md"""
        with self.lock:
            paths = sorted(self.sched_files)
            root = self.root
        year = f"{dt.date.today().year}"
        for p in paths:
            if os.path.basename(p).startswith(year):
                return p
        if paths:
            return paths[0]
        return os.path.join(root, "일정.md") if root and os.path.isdir(root) else ""

    def sched_doc(self):
        path = self.sched_doc_path()
        if not path:
            return {"exists": False, "no_dir": True}
        if not os.path.exists(path):
            return {"exists": False, "title": os.path.splitext(os.path.basename(path))[0]}
        st = os.stat(path)
        return {"exists": True, "title": os.path.splitext(os.path.basename(path))[0],
                "mtime": st.st_mtime_ns // 1_000_000, "content": read_text(path)}

    def write_sched_doc(self, content, base_mtime=None, new_only=False, stamp=None):
        with self.write_lock:
            # 경로도 잠금 안에서 한 번만 정함 (그 사이 올해 일정 파일이 생겨도 검사·꼬리표·쓰기가 같은 파일)
            path = self.sched_doc_path()
            if not path:
                raise OSError("스페이스 폴더가 지정되지 않았습니다.")
            exists = os.path.exists(path)
            if exists and (new_only or (base_mtime is not None and os.stat(path).st_mtime_ns // 1_000_000 != base_mtime)):
                return None
            if stamp:
                try:
                    content = stamp(read_text(path) if exists else "", content)
                except OSError:
                    pass   # 이전 내용을 못 읽으면 꼬리표만 건너뜀 (저장은 진행)
            atomic_write(path, content)
        return self.sched_doc()

    def month(self, year, month):
        with self.lock:
            return {
                d.isoformat(): {"done": s[0], "total": s[1]}
                for d, s in self.summary.items()
                if d.year == year and d.month == month
            }


def publish(store, hub):
    """폴더를 다시 읽고 쌓인 변경을 접속자에게 알림 (감시 루프·저장 직후 공용)"""
    store.refresh()
    dates, sched = store.pop_changes()
    if dates:
        hub.broadcast("changed", {"dates": dates})
    if sched:
        hub.broadcast("schedule", {})


# ---------------------------------------------------------------- clients / SSE
class Hub:
    """SSE 구독자 관리 (개인 스페이스 창 / 팀스페이스 팀원)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = []  # (ip, queue)

    def subscribe(self, ip):
        q = queue.Queue()
        with self.lock:
            self.subscribers.append((ip, q))
        return q

    def unsubscribe(self, ip, q):
        with self.lock:
            self.subscribers = [s for s in self.subscribers if s[1] is not q]

    def broadcast(self, event, data):
        with self.lock:
            targets = list(self.subscribers)
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for _, q in targets:
            q.put(msg)


class Ctx:
    """요청 하나의 접속자 정보"""

    def __init__(self, ip, owner):
        self.ip = ip
        self.owner = owner      # 같은 PC (= 나): 루프백 + 주인 비밀값


class Handler(BaseHTTPRequestHandler):
    server_version = "dogani/1.0"
    share: "ShareServer" = None

    def log_message(self, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if _web_origin(self.headers):   # 웹페이지 사전 요청에는 CORS 허용 헤더를 주지 않음
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         f"{KEY_HEADER}, {HOST_HEADER}, {TOKEN_HEADER}, {USER_HEADER}, {OWNER_HEADER}, Content-Type")
        self.end_headers()

    def _check(self):
        """공통 접속 검사. 통과하면 Ctx, 아니면 응답을 보내고 None."""
        ip = self.client_address[0]
        # dogani 프로그램만 접속 허용 (웹브라우저로 주소만 쳐서 보는 것을 막음)
        # 웹페이지에서 온 요청(Origin 이 http/https) 은 키가 맞아도 거부 → 브라우저 탭이 127.0.0.1 로 노트를 읽는 것 방지
        if self.headers.get(KEY_HEADER) != CLIENT_KEY or _web_origin(self.headers):
            self._send_json({"error": "client_only", "message": "dogani 로만 볼 수 있습니다."}, 403)
            return None
        share = self.share
        return Ctx(ip, share.is_owner(ip, self.headers.get(OWNER_HEADER, "")))

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_NOTE_BYTES:
            raise OverflowError
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    # ------------------------------------------------------------ routing
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        ctx = self._check()
        if not ctx:
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            parts = url.path.split("/", 4)   # "", "api", "s", id, 나머지
            if url.path == "/api/join" and method == "POST":
                self._join(ctx)
            elif url.path == "/api/me":
                self._whoami()
            elif len(parts) >= 4 and parts[1] == "api" and parts[2] == "s":
                host = self.share.spaces.get(parts[3])
                if not host:
                    return self._send_json({"error": "no_space", "message": "이 PC 에 그 스페이스가 없습니다."}, 404)
                self._space_api(host, method, "/" + (parts[4] if len(parts) > 4 else ""), qs, ctx)
            else:
                self._send_json({"error": "not found"}, 404)
        except TeamError as e:
            self._send_json({"error": e.code, "message": e.message}, e.status)
        except OverflowError:
            self._send_json({"error": "too_large"}, 413)
        except (KeyError, ValueError, TypeError, AttributeError):
            self._send_json({"error": "bad request"}, 400)

    def _join(self, ctx):
        """초대장 코드 → 그 코드를 가진 스페이스에 참여 (코드는 60비트 무작위라 스페이스끼리 겹치지 않음)"""
        body = self._body()
        share = self.share
        # 내 스페이스에 내가 참여하면 팀원이 하나 더 생김 (초대장도 써 버림).
        # 내 LAN 주소로 보내면 비밀값이 안 붙으므로, 참여하는 쪽이 보낸 자기 서버 id 로도 가림
        if ctx.owner or str(body.get("instance") or "") == share.instance_id:
            raise TeamError("own_space", "이 PC 의 스페이스입니다.")
        share.join_gate.enter(ctx.ip)
        host = next((h for h in list(share.spaces.values()) if h.has_invite(body.get("code"))), None)
        if not host:
            raise bad_code()
        try:
            res = host.join(ctx.ip, body.get("code"), body.get("name"))
        except Exception as e:
            # 확인한 사이 만료·사용된 초대장은 센 그대로, 이름 겹침·저장 실패 등 초대장 탓이 아닌 실패는 돌려줌
            if not (isinstance(e, TeamError) and e.code == "bad_code"):
                share.join_gate.refund(ctx.ip)
            if isinstance(e, OSError):   # team.json 저장 실패: 연결을 끊지 말고 이유를 알림
                raise TeamError("write_failed", f"참여 정보를 저장하지 못했습니다.\n({e})", 500) from e
            raise
        share.join_gate.ok(ctx.ip)
        res.update(pub=host.pub, team=host.name, space=host.space_id, pc=share.pc_name)
        self._send_json(res)

    def _whoami(self):
        """토큰만 아는 예전 설정(스페이스 id 없음)용: 토큰이 맞는 스페이스를 찾아 id 를 알려 줌"""
        token = self.headers.get(TOKEN_HEADER, "")
        for host in list(self.share.spaces.values()):
            if token and host.member(token):
                return self._send_json({"space": host.space_id, "pub": host.pub, "team": host.name})
        self._send_json({"error": "not_member", "message": "팀원만 볼 수 있습니다."}, 401)

    # ------------------------------------------------------------ 스페이스 하나 (/api/s/<id>/*)
    def _space_api(self, team, method, path, qs, ctx):
        if path == "/hello":
            # 토큰 없이 받는 호스트 확인: nonce 에 서명해 줌 → 팀원은 공개키로 확인한 뒤에만 토큰을 보냄
            # (PC 이름으로 바뀐 IP 를 찾을 때 이름을 가로챈 PC 에 토큰을 넘기지 않게)
            return self._send_json({"app": "dogani", "team": team.name, "host": self.share.name, "pub": team.pub,
                                    "sig": team.prove(qs.get("nonce", [""])[0])})
        # 이 PC(주인)는 토큰 없이 관리자, 다른 PC 는 이 스페이스의 토큰으로만 (내보낸 사람·다시 초대도 바로 반영)
        token = self.headers.get(TOKEN_HEADER, "")
        mid = team.owner_id if ctx.owner else (team.member(token) if token else None)
        if not mid:
            return self._send_json({"error": "not_member", "message": "팀원만 볼 수 있습니다."}, 401)
        if path == "/me":
            # 팀원 PC 가 주기적으로 보내는 신호 (접속 중 표시) → 팀원 목록을 받는다
            team.touch(mid)
            members = team.members()
            me = next((m for m in members if m["id"] == mid), None)
            # pc = 호스트 PC 이름: 호스트 IP 가 바뀌면 팀원이 이 이름으로 다시 찾음
            return self._send_json({"me": me, "members": members, "pub": team.pub, "team": team.name,
                                    "pc": self.share.pc_name})
        if path == "/profile" and method == "POST":
            body = self._body()
            name = team.update_profile(mid, body.get("name"))
            return self._send_json({"name": name})
        writable = team.can_write(mid)
        if path == "/info":
            return self._send_json({"app": "dogani", "name": team.name, "writable": writable,
                                    "today": dt.date.today().isoformat()})
        if path == "/events":
            return self._stream(team.hub, ctx)
        if path == "/schedule" and method == "GET":
            return self._send_json({"events": team.with_authors(team.store.schedule()), "writable": writable,
                                    "decos": team.with_authors(team.store.decorations()),
                                    # 날짜 붙은 할일은 주인 달력에만 (내 노트 할일은 남에게 보이지 않음)
                                    "tasks": team.store.dated_tasks() if ctx.owner else [],
                                    "members": team.mention_names(),   # @멤버 칩 후보 (팀원·그룹·all)
                                    "groups": team.group_cards()})     # 사이드바 멤버 그룹 칩 (구성원·권한 미리보기)
        # 팀원이 생긴 뒤부터 새로 쓰거나 고친 줄에 작성자 꼬리표 (혼자 쓰는 동안은 노트가 예전과 같음)
        stamp = None if team.solo else (lambda old, new: schedule.stamp(old, new, mid, team.member_ids()))
        self._space(method, path, qs, team.store, team.hub, writable=writable, stamp=stamp)

    # ------------------------------------------------------------ 스페이스 공통 (개인·팀)
    def _space(self, method, path, qs, store, hub, writable, stamp=None):
        if method == "POST":
            if not writable:
                return self._send_json({"error": "read_only"}, 403)
            body = self._body()
            content = body["content"]
            if not isinstance(content, str):
                raise ValueError
            try:
                if path == "/note":
                    # base_mtime: null(키는 있음) = 새 파일로 알고 씀. 키가 없으면(예전 방식) 확인 없이 덮어씀
                    new_only = "base_mtime" in body and body["base_mtime"] is None
                    note = store.write(dt.date.fromisoformat(body["date"]), content, body.get("base_mtime"), new_only)
                elif path == "/sched-doc":
                    # stamp = 팀스페이스: 새로 쓰거나 고친 줄 끝에 작성자 꼬리표 (저장 잠금 안에서 계산)
                    note = store.write_sched_doc(content, body.get("base_mtime"),
                                                 "base_mtime" in body and body["base_mtime"] is None, stamp)
                else:
                    return self._send_json({"error": "not found"}, 404)
            except OSError as e:
                return self._send_json({"error": "write_failed", "message": str(e)}, 500)
            if note is None:
                return self._send_json({"error": "conflict"}, 409)
            publish(store, hub)
            return self._send_json(note)
        if path == "/note":
            d = dt.date.fromisoformat(qs.get("date", [dt.date.today().isoformat()])[0])
            self._send_json(store.note(d))
        elif path == "/sched-doc":
            self._send_json(store.sched_doc())
        elif path == "/month":
            y, m = map(int, qs["ym"][0].split("-"))
            self._send_json({"ym": f"{y:04d}-{m:02d}", "days": store.month(y, m)})
        else:
            self._send_json({"error": "not found"}, 404)

    def _stream(self, hub, ctx):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = hub.subscribe(ctx.ip)
        try:
            self.wfile.write(b"retry: 3000\nevent: hello\ndata: {}\n\n")
            self.wfile.flush()
            while not self.server.stopping:
                try:
                    msg = q.get(timeout=HEARTBEAT_INTERVAL)
                except queue.Empty:
                    msg = "event: ping\ndata: {}\n\n"   # 주석이 아닌 이벤트: 받는 쪽이 끊김을 알아챔 (app.js 감시)
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            hub.unsubscribe(ctx.ip, q)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    stopping = False

    def server_bind(self):
        # HTTPServer.server_bind 는 socket.getfqdn(DNS 역조회)을 불러 느린 망에서 켜기를 몇 분씩 막음. server_name 은 안 씀
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def close_later(self):
        """serve_forever 를 멈추고 듣는 소켓까지 닫음. shutdown 은 루프가 끝날 때까지 기다리므로 다른 스레드에서"""
        def run():
            self.shutdown()
            self.server_close()
        threading.Thread(target=run, daemon=True).start()


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # 실제 패킷은 나가지 않음
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class JoinGate:
    """초대장 참여 시도 제한 (IP 마다, 스페이스와 상관없이): FAIL_LIMIT 번 틀리면 마지막 시도부터 FAIL_LOCK 초 잠금.
    - 시도는 들어올 때(enter) 미리 셈: 검사와 셈이 따로면 동시에 보낸 요청이 모두 검사를 통과해 한도를 넘김
    - 횟수는 FAIL_WINDOW 동안 시도가 없어야 0: 잠금만 지나면 0 이면 61초마다 FAIL_LIMIT 번씩 계속 시도할 수 있음.
      잠금이 풀린 뒤 또 틀리면 바로 다시 잠김 (첫 FAIL_LIMIT 번 뒤로는 FAIL_LOCK 초에 한 번꼴)"""
    FAIL_WINDOW = INVITE_TTL   # 초대장 수명만큼

    def __init__(self):
        self.lock = threading.Lock()
        self.fails = {}   # ip -> [횟수, 마지막 시도 시각]

    def enter(self, ip):
        """시도 하나를 셈. 잠겨 있으면 TeamError(429)"""
        with self.lock:
            now, f = time.time(), self.fails.get(ip)
            if f and now - f[1] > self.FAIL_WINDOW:
                f = None
            if f and f[0] >= FAIL_LIMIT and now < f[1] + FAIL_LOCK:
                raise TeamError("locked", f"여러 번 틀려서 {FAIL_LOCK}초 동안 잠겼습니다.", 429)
            self.fails[ip] = [(f[0] if f else 0) + 1, now]

    def refund(self, ip):
        """틀린 초대장이 아닌 실패(이름 겹침 등) → 센 시도를 돌려줌"""
        with self.lock:
            f = self.fails.get(ip)
            if f:
                f[0] = max(0, f[0] - 1)

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


class ShareServer:
    """HTTP 서버 + 폴더 감시 (이 PC 에서 연 스페이스 여러 개). start() 가 포트를 못 열면 OSError."""

    def __init__(self, port, name=""):
        self.port = port
        self.ip = local_ip()   # 초대장 안내에 보이는 주소용 (주인 판정에는 쓰지 않음)
        self.owner_secret = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(8)   # 비밀 아님: 남이 알아도 '내 스페이스 참여 거부'만 가능
        self.pc_name = socket.gethostname()
        self.name = name or self.pc_name   # 표시 이름 (트레이 > 내 이름)
        self.spaces = {}          # 스페이스 id -> TeamHost (요청 스레드가 읽으므로 통째로 바꿔 끼움)
        self.join_gate = JoinGate()
        self.running = False
        self.httpd = None

    @property
    def address(self):
        return f"{self.ip}:{self.port}"

    def is_owner(self, ip, secret):
        """루프백 + 주인 비밀값이 맞는 요청 = 나 (모든 스페이스의 주인)."""
        return ip in ("127.0.0.1", "::1") and hmac.compare_digest(str(secret).encode(), self.owner_secret.encode())

    def add_space(self, host):
        self.spaces = {**self.spaces, host.space_id: host}

    def remove_space(self, sid):
        host = self.spaces.get(sid)
        self.spaces = {k: v for k, v in self.spaces.items() if k != sid}
        if host:
            host.hub.broadcast("reload", {})

    def start(self):
        Handler.share = self
        self.httpd = _HTTPServer(("0.0.0.0", self.port), Handler)
        self.running = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.httpd:
            self.httpd.stopping = True
            self.httpd.close_later()

    def _watch_loop(self):
        while self.running:
            for host in list(self.spaces.values()):
                publish(host.store, host.hub)
            time.sleep(POLL_INTERVAL)
