"""
공휴일 받기 (대체·임시공휴일 포함). 키 없이 받는 공개 JSON, PC 마다 따로 받는다.

- 출처: holidays-kr (한국천문연구원 특일정보 기반) https://holidays.hyunbin.page/2026.json
  모양: {"2026-01-01": ["1월 1일"], ...}
- %APPDATA%\\dogani\\holidays\\<연도>.json 에 저장 {"fetched": 초, "days": {...}}
- 저장본이 12시간보다 오래됐으면 뒤에서 새로 받음 (못 받으면 저장본 그대로)
- 데이터만 받음 (코드 실행 없음)
"""
import json
import os
import re
import threading
import time
import urllib.request

URL = "https://holidays.hyunbin.page/{year}.json"
STALE = 12 * 3600
RETRY = 10 * 60   # 실패 후 다시 시도까지
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 법정 명칭 → 흔히 쓰는 이름 (괄호 안 대체공휴일 이름에도 적용)
NICK = {"기독탄신일": "성탄절", "1월 1일": "신정", "부처님 오신 날": "석가탄신일", "3ㆍ1절": "삼일절"}


def _nick(name):
    for k, v in NICK.items():
        name = name.replace(k, v)
    return name

_lock = threading.Lock()
_busy = set()
_failed = {}   # year -> 실패 시각


def _path(cfg_dir, year):
    return os.path.join(cfg_dir, "holidays", f"{year}.json")


def _clean(data, year):
    """받은 JSON → {날짜: [이름]} (모양이 다르면 ValueError)"""
    if not isinstance(data, dict):
        raise ValueError("bad shape")
    out = {}
    for d, names in data.items():
        if not (isinstance(d, str) and _DATE_RE.match(d) and d.startswith(str(year))):
            continue
        if isinstance(names, str):
            names = [names]
        if isinstance(names, list):
            out[d] = [str(n)[:40] for n in names if str(n).strip()][:4]
    return out


def _load(cfg_dir, year):
    try:
        with open(_path(cfg_dir, year), encoding="utf-8") as f:
            c = json.load(f)
        return c.get("fetched", 0), c.get("days", {})
    except (OSError, ValueError, AttributeError):
        return 0, None


def _fetch(cfg_dir, year):
    try:
        req = urllib.request.Request(URL.format(year=year), headers={"User-Agent": "dogani"})
        with urllib.request.urlopen(req, timeout=10) as r:
            days = _clean(json.loads(r.read(512 * 1024).decode("utf-8")), year)
        p = _path(cfg_dir, year)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched": time.time(), "days": days}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except (OSError, ValueError):
        _failed[year] = time.time()
    finally:
        with _lock:
            _busy.discard(year)


def get(cfg_dir, year):
    """{days: {날짜: [이름]}, pending: 지금 받는 중} — 저장본을 바로 주고, 오래됐으면 뒤에서 새로 받음"""
    year = int(year)
    if not 1900 < year < 3000:
        return {"days": {}, "pending": False}
    fetched, days = _load(cfg_dir, year)
    stale = time.time() - fetched > STALE and time.time() - _failed.get(year, 0) > RETRY
    with _lock:
        if stale and year not in _busy:
            _busy.add(year)
            threading.Thread(target=_fetch, args=(cfg_dir, year), daemon=True).start()
        pending = year in _busy
    days = {d: [_nick(n) for n in ns] for d, ns in (days or {}).items() if isinstance(ns, list)}
    return {"days": days, "pending": pending}
