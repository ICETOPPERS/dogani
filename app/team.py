"""
팀스페이스 (팀원이 같이 쓰는 폴더 + 팀원 식별)

- 팀 호스트: 팀원 중 한 사람의 dogani 가 맡는다 (켜 두는 시간이 긴 PC 추천).
  팀스페이스 = 호스트 PC 의 폴더 하나 (개인 스페이스와 같은 형식, space.py). 노트·일정은 평범한 md 파일
  팀 설정(이름·팀원)은 스페이스의 .dogani/team.json, 서명 키·초대장은 호스트 PC 의 %APPDATA%\\dogani\\team\\<스페이스id>.json
  (스페이스 폴더가 복사·동기화돼도 비밀값은 따라가지 않음)
- 인증 (IT 회사가 아닌 사무실 수준):
  * 팀 참여 = 호스트 주소 + 초대장 코드(사람마다 다름, 12자리, 한 번 쓰면 폐기, 15분 뒤 만료).
    이름은 따로 안 묻고 그 PC 의 '내 이름'(기본 PC 이름)을 씀. 다시 초대는 팀에 있던 이름 유지
  * 한 번 참여하면 PC 에 토큰을 저장해서 다시 묻지 않음. PC 를 바꾸면 호스트가 그 사람 자리로 다시 초대
    (다시 초대로 들어오면 예전 PC 의 토큰은 무효)
  * 초대장 코드를 5번 틀리면 그 IP 는 1분 동안 참여 금지
  * 토큰 = 팀원ID.발급시각.Ed25519 서명. 서명 키(개인키)는 호스트 PC 에만 있고 호스트가 직접 검증
  * 내보내기 = 팀원 목록에서 삭제, 다시 초대로 참여 = min_iat 올림
- 그룹: groups = {그룹id: {name, perm}}, 팀원 groups = [그룹id]. 한 사람이 여러 그룹에 들 수 있음.
  일정의 @그룹 = 그 그룹 팀원 모두, @all = 팀원 모두 (근태·멤버 필터에서 펼침). 그룹 이름은 팀원 이름·all 과 겹치지 않음
- 권한: 관리자는 항상 write. 팀원 perm 이 read|write 로 적혀 있으면 그것(개인 지정), 없으면 그룹을 따름
  (그룹 중 하나라도 write 면 write, 그룹이 있는데 모두 read 면 read, 그룹이 없으면 스페이스 기본 권한 default_perm).
  default_perm: 새로 만든 스페이스는 read (보기만으로 시작해 쓸 사람만 열어 줌), 키가 없는 예전 파일은 write (예전 동작 유지)
  서버가 요청마다 확인 (토큰엔 안 넣음 → 바꾸면 바로 반영)
- 작성자: 팀스페이스 일정 저장 때 새로 쓰거나 고친 줄 끝에 " ~작성id>수정id" 꼬리표 (schedule.stamp, 이름은 볼 때 team.json 에서)
"""
import datetime as dt
import os
import secrets
import threading
import time
from functools import lru_cache

import ed25519
import space

INVITE_TTL = 15 * 60    # 초대장 유효 시간(초)
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 헷갈리는 0/O, 1/I 제외
INVITE_LEN = 12         # 32^12 ≈ 60비트
FAIL_LIMIT = 5
FAIL_LOCK = 60          # 초
ONLINE_WINDOW = 75      # 마지막 신호가 이 안이면 접속 중 (클라이언트는 20초마다 보냄)
NAME_MAX = 20
ALL_NAME = "all"        # @all = 팀원 모두 (예약된 이름, casefold 로 비교)

SCHEDULE_SEED = """# 팀 일정

> 한 줄 = 일정 하나:  `날짜, 제목, #태그, 장소, 메모`
> 날짜 **굵게** = 중요 · ==하이라이트== = 근태 (@이름 = 그 사람, 없으면 모두 쉬는 날) · *기울임* = 기간을 화살표로
> 예) `**261119** 15:00, 정기 점검, #전산, 서버실` · `261124-261126, 출장, #외근` · `==261231==, 창립기념일` · `==261120==, 오후반차, @홍길동`

"""


# ---------------------------------------------------------------- token
def public_key(sign_key_hex):
    return ed25519.public_key(bytes.fromhex(sign_key_hex)).hex()


def make_token(sign_key_hex, mid, iat=None):
    iat = int(iat if iat is not None else time.time())
    return f"{mid}.{iat}.{ed25519.sign(bytes.fromhex(sign_key_hex), f'{mid}.{iat}'.encode()).hex()}"


HELLO_PREFIX = b"dogani-hello:"   # 호스트 확인 서명 (토큰 서명 메시지 "id.iat" 와 섞이지 않게 접두어)
NONCE_LEN = 32                     # hex 글자 수 (16바이트)
PROVE_LIMIT, PROVE_WINDOW = 20, 10  # 호스트 확인 서명은 10초에 20번까지 (팀원은 호스트를 놓쳤을 때만 부름)


def verify_hello(pub_hex, nonce, sig_hex):
    """호스트가 nonce 에 서명한 값이 그 팀의 공개키로 맞는지 (토큰을 보내기 전 '진짜 우리 호스트' 확인)"""
    try:
        return bool(pub_hex) and ed25519.verify(bytes.fromhex(pub_hex), HELLO_PREFIX + nonce.encode(),
                                                bytes.fromhex(str(sig_hex or "")))
    except Exception:   # 상대가 보낸 값이 무엇이든 '확인 안 됨'으로 (작업 스레드가 죽지 않게)
        return False


def _shape_ok(token, members=None):
    """비싼 서명 검증 전에 싼 검사: 형식·서명 길이·팀원 목록에 있는 ID 인지 (아무 토큰으로 CPU 를 태우지 못하게)"""
    parts = (token or "").split(".")
    return len(parts) == 3 and len(parts[2]) == 128 and (not members or parts[0] in members)


@lru_cache(maxsize=256)   # 같은 토큰이 요청마다 오므로 서명 검증은 한 번만
def parse_token(pub_hex, token):
    """서명이 맞으면 (팀원ID, 발급시각), 아니면 None"""
    try:
        mid, iat, sig = (token or "").split(".")
        iat = int(iat)
        ok = ed25519.verify(bytes.fromhex(pub_hex), f"{mid}.{iat}".encode(), bytes.fromhex(sig))
    except ValueError:
        return None
    return (mid, iat) if ok else None


# ---------------------------------------------------------------- invite
def normalize_invite(code):
    """입력한 초대장 코드 → 대문자·구분자 제거 (XXXX-XXXX-XXXX 로 보여 주고, 띄어쓰기·소문자도 받음)"""
    return "".join(c for c in str(code or "").upper() if c.isalnum())


def format_invite(code):
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def _key(name):
    """이름 비교용: 대소문자·띄어쓰기 무시 (달력은 @이름 의 띄어쓰기를 지우므로 '전 산' 과 '전산' 은 같은 @전산)"""
    return "".join(str(name or "").split()).casefold()


def _taken(other):
    """_find_name 결과 → 이름 겹침 오류 문구 ("" = 그룹 이름·all)"""
    if other == "":
        return TeamError("name_taken", "멤버 그룹 이름이나 all 은 팀원 이름으로 쓸 수 없습니다.", 409)
    return None


def clean_name(name):
    name = " ".join(str(name or "").split())[:NAME_MAX]
    return name


class TeamError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------- host
class TeamHost:
    """팀스페이스 + 팀 데이터: 스페이스/.dogani/team.json(이름·팀원) + 키 파일(서명 키·초대장, 이 PC 에만)"""

    PRIVATE = ("sign_key", "invites")   # 스페이스에 두지 않는 값

    def __init__(self, root, key_dir, owner_name):
        from server import Hub, NoteStore  # 순환 import 방지
        self.lock = threading.Lock()
        self.root = self.notes_dir = root
        fresh = not any(n != space.META_DIR for n in os.listdir(root))   # 새로 만든 빈 폴더
        meta = space.open_space(root)
        self.space_id = meta["id"]
        self.path = os.path.join(space.meta_dir(root), "team.json")
        self.key_path = os.path.join(key_dir, f"{meta['id']}.json")
        self.data = self._load()
        if self.data.pop("_dirty", False):   # 새 서명 키는 바로 저장 (안 그러면 재시작마다 키가 바뀜)
            self._save()
        self.pub = public_key(self.data["sign_key"])
        self.fails = {}   # ip -> [횟수, 잠금 해제 시각]
        self.proofs = []  # 최근 호스트 확인 서명 시각 (prove 횟수 제한)
        if fresh:   # 빈 폴더에만 예시 일정 파일 (이미 쓰던 노트 폴더에는 파일을 만들지 않음)
            with open(os.path.join(root, "일정.md"), "w", encoding="utf-8") as f:
                f.write(SCHEDULE_SEED)
        self.store = NoteStore()
        self.store.set_root(root)
        self.store.refresh()
        self.store.pop_changes()   # 처음 읽은 것은 알릴 필요 없음
        self.hub = Hub()
        self.owner_id = self._ensure_owner(owner_name)

    # -- storage
    def _load(self):
        data = {**(space.read_json(self.path) or {}), **(space.read_json(self.key_path) or {})}
        data.setdefault("name", "팀스페이스")
        if not os.path.exists(self.path):   # 새 스페이스만 보기만으로 시작 (예전 team.json 은 키가 없으면 write)
            data["default_perm"] = "read"
        if "sign_key" not in data:   # 새 팀: 서명 키를 만들고 바로 저장
            data["sign_key"] = secrets.token_hex(32)
            data["_dirty"] = True
        data.setdefault("members", {})
        data.setdefault("invites", {})
        if not isinstance(data.get("groups"), dict):   # 그룹 이전 버전·깨진 값
            data["groups"] = {}
        return data

    def _save(self):
        # 키 파일 먼저 (스페이스만 저장되고 키가 없으면 다음 실행에 새 키 = 모든 토큰 무효)
        space.write_json(self.key_path, {k: self.data[k] for k in self.PRIVATE})
        space.write_json(self.path, {k: v for k, v in self.data.items() if k not in self.PRIVATE})

    @property
    def name(self):
        return self.data["name"]

    def rename_team(self, name):
        name = clean_name(name)
        if name:
            with self.lock:
                self.data["name"] = name
                self._save()

    @property
    def solo(self):
        """팀원 없이 나 혼자 (다른 PC 에 열리지 않고, 일정 줄에 작성자 꼬리표를 붙이지 않음)"""
        with self.lock:
            return len(self.data["members"]) <= 1

    def has_invite(self, code):
        code = normalize_invite(code)
        with self.lock:
            v = self.data["invites"].get(code)
            return bool(v) and v.get("exp", 0) > time.time()

    # -- members
    def _find_name(self, name):
        """그 이름을 쓰는 팀원 id. 그룹 이름·all 이면 "" (@이름 이 겹치지 않게 팀원도 못 씀), 없으면 None"""
        key = _key(name)
        for mid, m in self.data["members"].items():
            if _key(m["name"]) == key:
                return mid
        if key == ALL_NAME or self._find_group(name):
            return ""
        return None

    def _find_group(self, name):
        key = _key(name)
        return next((gid for gid, g in self.data["groups"].items() if _key(g["name"]) == key), None)

    def _ensure_owner(self, name):
        """호스트 PC 주인은 초대장 없이 자동으로 관리자 팀원"""
        with self.lock:
            for mid, m in self.data["members"].items():
                if m.get("admin"):
                    return mid
            name = clean_name(name) or "관리자"
            if self._find_name(name) is not None:
                name += "(관리자)"
            mid = secrets.token_hex(4)
            self.data["members"][mid] = {"name": name, "admin": True, "min_iat": 0,
                                         "joined": dt.datetime.now().isoformat(timespec="seconds"),
                                         "addr": "", "last_seen": 0}
            self._save()
            return mid

    def owner_token(self):
        return make_token(self.data["sign_key"], self.owner_id)

    def prove(self, nonce):
        """팀원이 보낸 nonce 에 서명 → 팀원은 토큰을 보내기 전에 공개키로 이 PC 가 진짜 호스트인지 확인 (verify_hello)"""
        nonce = str(nonce or "")
        if len(nonce) != NONCE_LEN or any(c not in "0123456789abcdef" for c in nonce):
            return ""
        now = time.time()
        with self.lock:   # 인증 전 요청이라 서명(파이썬 연산) 횟수 제한: 아무나 반복 호출로 CPU 를 태우지 못하게
            self.proofs = [t for t in self.proofs if now - t < PROVE_WINDOW]
            if len(self.proofs) >= PROVE_LIMIT:
                return ""
            self.proofs.append(now)
        return ed25519.sign(bytes.fromhex(self.data["sign_key"]), HELLO_PREFIX + nonce.encode()).hex()

    def _limit(self, ip):
        f = self.fails.get(ip)
        if f and f[0] >= FAIL_LIMIT and time.time() < f[1]:
            raise TeamError("locked", f"여러 번 틀려서 {FAIL_LOCK}초 동안 잠겼습니다.", 429)

    def _fail(self, ip, code, message):
        f = self.fails.setdefault(ip, [0, 0])
        if time.time() > f[1]:
            f[0] = 0 if f[0] >= FAIL_LIMIT else f[0]
        f[0] += 1
        f[1] = time.time() + FAIL_LOCK
        raise TeamError(code, message, 401)

    # -- invites
    def _purge(self):
        """만료된 초대장 정리 (lock 안에서 부름) → 바뀌었으면 True"""
        now = time.time()
        old = [c for c, v in self.data["invites"].items() if v.get("exp", 0) <= now]
        for c in old:
            del self.data["invites"][c]
        return bool(old)

    def _drop_invites(self, mid):
        """그 팀원 자리로 만든 초대장 모두 삭제 (lock 안에서 부름)"""
        for c in [c for c, v in self.data["invites"].items() if v.get("mid") == mid]:
            del self.data["invites"][c]

    def create_invite(self, mid=None):
        """일회용 초대장 → 코드(XXXX-XXXX-XXXX). mid 를 주면 그 팀원 자리로 다시 초대(PC 바꿈)"""
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LEN))
        with self.lock:
            m = self.data["members"].get(mid) if mid else None
            if mid and (not m or m.get("admin")):
                raise TeamError("not_member", "팀원이 아닙니다.", 404)
            self._purge()
            if mid:
                self._drop_invites(mid)   # 다시 초대는 한 사람당 한 장만 살아 있게
            self.data["invites"][code] = {"exp": int(time.time()) + INVITE_TTL, "mid": mid or ""}
            self._save()
        return format_invite(code)

    def cancel_invite(self, code):
        with self.lock:
            if self.data["invites"].pop(normalize_invite(code), None) is not None:
                self._save()

    def invites(self):
        """아직 안 쓴 초대장 [{code, label, left(초)}], 남은 시간 짧은 순"""
        now = time.time()
        with self.lock:
            if self._purge():
                self._save()
            out = []
            for c, v in self.data["invites"].items():
                m = self.data["members"].get(v.get("mid") or "")
                out.append({"code": format_invite(c), "left": int(v["exp"] - now), "mid": v.get("mid") or "",
                            "label": f"{m['name']} 다시 초대" if m else "새 팀원"})
        return sorted(out, key=lambda i: i["left"])

    def join(self, ip, code, name):
        """초대장 코드 + 그 PC 의 '내 이름' → 토큰. 성공하면 초대장은 폐기 (이름이 겹치면 초대장은 남음)"""
        self._limit(ip)
        code = normalize_invite(code)
        name = clean_name(name)
        with self.lock:
            purged = self._purge()
            inv = self.data["invites"].get(code) if len(code) == INVITE_LEN else None
            if inv is not None:
                mid = inv.get("mid") or ""
                if mid:
                    m = self.data["members"].get(mid)
                    if not m:   # 그 사이 내보냄 (보통은 kick 이 초대장도 지움)
                        del self.data["invites"][code]
                        self._save()
                        raise TeamError("bad_code", "초대장 대상이 팀에 없습니다. 관리자에게 새 초대장을 받으세요.", 404)
                    self._drop_invites(mid)   # 이름은 팀에 있던 그대로 (새 PC 는 받아서 '내 이름'으로 씀)
                    iat = int(time.time()) + 1
                    m["min_iat"] = iat   # 예전 PC 의 토큰(같은 1초 것 포함)은 무효, 새 토큰만 통과
                else:
                    if not name:
                        raise TeamError("bad_name", "이름이 비어 있습니다.")
                    other = self._find_name(name)
                    if other == "":
                        raise _taken(other)
                    if other is not None:
                        raise TeamError("name_taken", f"'{name}' 은(는) 이미 팀에 있는 이름입니다. "
                                        "본인이면 관리자에게 '다시 초대'를 부탁하세요.", 409)
                    mid = secrets.token_hex(4)
                    self.data["members"][mid] = {
                        "name": name, "admin": False, "min_iat": 0, "addr": "", "last_seen": 0,
                        "joined": dt.datetime.now().isoformat(timespec="seconds")}
                    iat = None
                self.data["invites"].pop(code, None)
                self._save()
                self.fails.pop(ip, None)
                return self._session(mid, iat)
            if purged:
                self._save()
        self._fail(ip, "bad_code", "초대장 코드가 맞지 않거나 만료됐습니다. (15분 안에 한 번만 쓸 수 있음)")

    def _session(self, mid, iat=None):
        token = make_token(self.data["sign_key"], mid, iat)
        return {"token": token, "id": mid, "name": self.data["members"][mid]["name"]}

    def member(self, token):
        """토큰 → 팀원 id (내보냈거나 다시 초대 이전 토큰이면 None)"""
        with self.lock:
            known = set(self.data["members"])
        if not _shape_ok(token, known):
            return None
        parsed = parse_token(self.pub, token)
        if not parsed:
            return None
        mid, iat = parsed
        with self.lock:
            m = self.data["members"].get(mid)
            if not m or iat < m.get("min_iat", 0):
                return None
        return mid

    def touch(self, mid, addr):
        with self.lock:
            m = self.data["members"].get(mid)
            if m:
                m["last_seen"] = time.time()
                if m.get("addr") != addr:
                    m["addr"] = addr
                    self._save()

    def update_profile(self, mid, name=None):
        with self.lock:
            m = self.data["members"].get(mid)
            if not m:
                raise TeamError("not_member", "팀원이 아닙니다.", 401)
            if name is not None:
                name = clean_name(name)
                if not name:
                    raise TeamError("bad_name", "이름을 입력하세요.")
                other = self._find_name(name)
                if other == "":
                    raise _taken(other)
                if other is not None and other != mid:
                    raise TeamError("name_taken", "다른 팀원이 쓰는 이름입니다.", 409)
                m["name"] = name
            self._save()
            return m["name"]

    def kick(self, mid):
        with self.lock:
            m = self.data["members"].get(mid)
            if m and not m.get("admin"):
                del self.data["members"][mid]
                self._drop_invites(mid)
                self._save()

    # -- 권한
    def _perm(self, m):
        """팀원 하나의 실제 권한 (lock 안에서 부름). 규칙은 파일 맨 위 docstring"""
        if m.get("admin"):
            return "write"
        if m.get("perm") in ("read", "write"):
            return m["perm"]
        gs = [self.data["groups"][g] for g in m.get("groups") or () if g in self.data["groups"]]
        if not gs:
            return self.default_perm
        return "write" if any(g.get("perm") != "read" for g in gs) else "read"

    def can_write(self, mid):
        with self.lock:
            m = self.data["members"].get(mid)
            return bool(m) and self._perm(m) == "write"

    def _changed(self):
        """권한·그룹이 바뀜 → 저장하고 팀원 화면이 /schedule 을 다시 받게 (쓰기 가능 여부·@그룹 펼침 갱신)"""
        self._save()
        self.hub.broadcast("schedule", {})

    def set_perm(self, mid, perm):
        """perm = read | write (개인 지정) | auto (그룹을 따름)"""
        if perm not in ("read", "write", "auto"):
            raise TeamError("bad_perm", "권한은 read, write, auto 중 하나입니다.")
        with self.lock:
            m = self.data["members"].get(mid)
            if not m or m.get("admin"):
                return
            if perm == "auto":
                m.pop("perm", None)
            else:
                m["perm"] = perm
            self._changed()

    @property
    def default_perm(self):
        return "read" if self.data.get("default_perm") == "read" else "write"

    def set_default_perm(self, perm):
        if perm not in ("read", "write"):
            raise TeamError("bad_perm", "권한은 read 또는 write 입니다.")
        with self.lock:
            self.data["default_perm"] = perm
            self._changed()

    # -- 그룹
    def _group_name(self, name, gid=None):
        """그룹 이름 검사 → 정리한 이름 (팀원 이름·다른 그룹·all 과 겹치면 TeamError)"""
        name = clean_name(name).replace(" ", "")   # @그룹 은 띄어쓰기 없이 한 단어
        if not name:
            raise TeamError("bad_name", "멤버 그룹 이름을 입력해 주세요.")
        if any(c in name for c in "@#,~"):
            raise TeamError("bad_name", "멤버 그룹 이름에는 @ # , ~ 를 쓸 수 없습니다.")
        other = self._find_name(name)
        if other is not None and not (gid and other == "" and self._find_group(name) == gid):
            raise TeamError("name_taken", f"'{name}' 은(는) 이미 쓰고 있는 이름입니다.", 409)
        return name

    def create_group(self, name, perm=None):
        """perm 을 안 주면 스페이스 기본 권한 (태그용으로 묶었는데 편집이 열리지 않게)"""
        with self.lock:
            name = self._group_name(name)
            gid = secrets.token_hex(4)
            perm = perm or self.default_perm
            self.data["groups"][gid] = {"name": name, "perm": "read" if perm == "read" else "write"}
            self._changed()
            return gid

    def rename_group(self, gid, name):
        with self.lock:
            if gid in self.data["groups"]:
                self.data["groups"][gid]["name"] = self._group_name(name, gid)
                self._changed()

    def set_group_perm(self, gid, perm):
        if perm not in ("read", "write"):
            raise TeamError("bad_perm", "권한은 read 또는 write 입니다.")
        with self.lock:
            g = self.data["groups"].get(gid)
            if g:
                g["perm"] = perm
                self._changed()

    def delete_group(self, gid):
        """그룹 삭제 + 팀원 목록에서도 뺌 (일정에 적힌 @그룹~id 는 적힌 이름 그대로 남음)"""
        with self.lock:
            if self.data["groups"].pop(gid, None) is None:
                return
            for m in self.data["members"].values():
                if gid in (m.get("groups") or ()):
                    m["groups"] = [g for g in m["groups"] if g != gid]
            self._changed()

    def set_member_group(self, mid, gid, on):
        """팀원을 그룹에 넣거나(on=True) 뺌. 관리자도 그룹에 들 수 있음 (@그룹 대상)"""
        with self.lock:
            m = self.data["members"].get(mid)
            if not m or gid not in self.data["groups"]:
                return
            gs = [g for g in m.get("groups") or () if g != gid] + ([gid] if on else [])
            if gs:
                m["groups"] = gs
            else:
                m.pop("groups", None)
            self._changed()

    def group_cards(self):
        """달력 사이드바 멤버 그룹 칩용: [{name, members: [{name, perm}]}] 이름순.
        권한은 그룹 설정이 아니라 그 팀원의 실제 권한 (개인 지정이 우선이라 미리보기가 실제와 같게)"""
        with self.lock:
            ms = self.data["members"].values()
            out = [{"name": g["name"],
                    "members": [{"name": m["name"], "perm": self._perm(m)} for m in ms if gid in (m.get("groups") or ())]}
                   for gid, g in self.data["groups"].items()]
        return sorted(out, key=lambda g: g["name"].casefold())

    def groups(self):
        """[{id, name, perm, members: [팀원id]}] 이름순"""
        with self.lock:
            out = [{"id": gid, "name": g["name"], "perm": g.get("perm", "write"),
                    "members": [mid for mid, m in self.data["members"].items() if gid in (m.get("groups") or ())]}
                   for gid, g in self.data["groups"].items()]
        return sorted(out, key=lambda g: g["name"].casefold())

    # -- 일정 작성자 (줄 끝 꼬리표 id → 이름)
    def with_authors(self, events):
        """일정 목록에 작성·수정 팀원 이름(who) 붙이고, id 붙은 @멤버·@그룹은 지금 이름으로. 꼬리표 없는 줄은 그대로.
        @그룹·@all 이 있으면 reach = 가리키는 팀원 이름 모두 (근태 '내 쉬는 날' 판정용)"""
        with self.lock:
            members, groups = self.data["members"], self.data["groups"]
            name = lambda i: (members.get(i) or {}).get("name", "(나간 팀원)") if i else ""
            out = []
            for ev in events:
                if ev.get("by"):
                    ev = {**ev, "who": {"by": name(ev["by"]), "mod": name(ev.get("mod"))}}
                if any(ev.get("member_ids") or ()):   # @이름~id → 지금 이름 (나간 팀원·지운 그룹은 적힌 이름 그대로)
                    ev = {**ev, "members": [(members.get(i) or groups.get(i) or {}).get("name", n)
                                            for n, i in zip(ev["members"], ev["member_ids"])]}
                reach = self._reach(ev.get("members") or (), ev.get("member_ids") or ())
                if reach is not None:
                    ev = {**ev, "reach": reach}
                out.append(ev)
            return out

    def _reach(self, names, ids):
        """@그룹·@all 을 팀원 이름으로 펼친 목록 (lock 안에서 부름). 그룹·all 이 없으면 None"""
        members, groups = self.data["members"], self.data["groups"]
        out, wide = [], False
        for n, i in zip(names, list(ids) + [""] * len(names)):
            # 팀원 id 가 아니면 그룹: id 로, 없거나 지운 그룹 id 면 이름으로 (같은 이름으로 다시 만든 그룹)
            gid = None if i in members else (i if i in groups else self._find_group(n))
            if _key(n) == ALL_NAME and i not in members:   # 예전에 'All' 이라는 팀원이 있었으면 그 사람
                hit = [m["name"] for m in members.values()]
            elif gid:
                hit = [m["name"] for m in members.values() if gid in (m.get("groups") or ())]
            else:
                out.append(n)
                continue
            wide = True
            out += hit
        return list(dict.fromkeys(out)) if wide else None

    def member_ids(self):
        """{이름.casefold(): 팀원id·그룹id}: 일정 저장 때 @이름·@그룹 에 ~id 붙이기용 (schedule.stamp).
        id 를 붙여 두면 그룹 이름을 바꿔도 일정이 따라감. @all 은 붙이지 않음"""
        with self.lock:
            ids = {g["name"].casefold(): gid for gid, g in self.data["groups"].items()}
            ids.update({m["name"].casefold(): mid for mid, m in self.data["members"].items()})
            return ids

    def mention_names(self):
        """@ 칩 후보: 팀원 이름 + 그룹 이름 + all"""
        with self.lock:
            return ([m["name"] for m in self.data["members"].values()] +
                    sorted(g["name"] for g in self.data["groups"].values()) + [ALL_NAME])

    def members(self):
        now = time.time()
        with self.lock:
            groups = self.data["groups"]
            out = [{"id": mid, "name": m["name"], "addr": m.get("addr", ""), "admin": bool(m.get("admin")),
                    "perm": self._perm(m), "perm_set": m.get("perm", "auto") if not m.get("admin") else "write",
                    "groups": [groups[g]["name"] for g in m.get("groups") or () if g in groups],
                    "group_ids": [g for g in m.get("groups") or () if g in groups],
                    "min_iat": m.get("min_iat", 0),
                    "online": now - m.get("last_seen", 0) < ONLINE_WINDOW or bool(m.get("admin")),
                    "last_seen": m.get("last_seen", 0)}
                   for mid, m in self.data["members"].items()]
        return sorted(out, key=lambda m: (not m["admin"], m["name"]))
