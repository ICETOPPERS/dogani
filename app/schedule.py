"""
일정 파일(예: "2026일정.md") 파싱. todo 폴더 안 어디에 있어도 찾는다.

한 줄 = 일정 하나:  일시, 제목, #태그, 장소, 메모
  **261119** 15:00, 과제 중간실사, #과제, 대회의실, 메모할내용
  261116-261120,고객사 교육 준비, #교육
  **261125** 9:00-12:00, 과제 발표평가, #과제, 외부
  ==261231==,창립기념일
  ==261127==,3시반반차에요, @홍길동

- 날짜: YYMMDD / YYYY-MM-DD, 기간은 시작-끝 또는 시작~끝 (끝은 YYMMDD, MMDD, DD 도 가능)
- 시간: 날짜 뒤에 "15:00" 또는 "9:00-12:00" (없어도 됨)
- 날짜를 **굵게** = 중요 표시, ==하이라이트== = 근태 (@멤버 = 그 사람, 없으면 모두 쉬는 날. 판정은 calendar.js),
  *기울임* = 기간을 화살표(←──→)로 표시
- "10:00-10:30?" 처럼 시간 끝에 ? = 그 사이 언젠가 (정확한 시각 모름)
- "-261119" = 시작 미정, 마감만 / "261119-" = 시작만, 끝 미정 (달력에서 페이드로 표시)
- 장식 줄 "261119 ✦ 글자" = 일정이 아니라 그날 칸에 옅게 붙는 꾸밈 글자 (deco: True, 할일 창에서 끌어 놓아 만듦)
- 태그는 #으로 시작하는 칸(여러 개 가능), 같은 칸의 @이름 = 멤버 ("#근태 @홍길동", "@홍길동" 만도 됨). 태그가 아닌 칸은 순서대로 장소, 메모 (장소 없이 메모만: "제목, , 메모")
"""
import datetime as dt
import re

SCHEDULE_FILE_RE = re.compile(r"^(?:\d{4}\s*)?일정\.md$", re.I)

_DATE = r"(\d{6}|\d{4}-\d{2}-\d{2})"
_FIRST_RE = re.compile(_DATE + r"(?:\s*[-~]\s*(\d{4}-\d{2}-\d{2}|\d{6}|\d{4}|\d{2})(?!\d))?\s*(.*)$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?:\s*[-~]\s*\d{1,2}:\d{2})?\??$")
_OPEN_START_RE = re.compile(r"^[-~]\s*" + _DATE + r"(?!\d)\s*(.*)$")       # -261119 : 시작 미정, 마감만
_OPEN_END_RE = re.compile(r"^" + _DATE + r"\s*[-~](?=\s*$|\s+\d{1,2}:|\s*[^\d\s])\s*(.*)$")       # 261119- : 시작만, 끝 미정
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[.\]\s+)?")
_MARK_RE = re.compile(r"\*|==")  # 굵게(**)·기울임(*)·하이라이트(==) 기호
_DECO_RE = re.compile(_DATE + r"\s*✦\s*(.*)$")   # 장식 줄 (글자가 비어도 장식: 일정 "✦" 로 둔갑 안 함)
_MEM_RE = re.compile(r"^@(.+?)~([0-9a-f]{8})$")  # 멤버 "@이름~팀원id" (팀스페이스, 화면엔 id 로 찾은 현재 이름)
_WHO_RE = re.compile(r"\s+~([0-9a-f]{8})(?:>([0-9a-f]{8}))?\s*$")  # 줄 끝 작성자 꼬리표 " ~작성id" / " ~작성id>수정id" (팀스페이스)


def is_schedule_file(name):
    return bool(SCHEDULE_FILE_RE.match(name))


def name_key(name):
    """@이름 비교용 (팀원·그룹 이름 겹침, ~id 붙이기, 달력 칩): 공백·@·#·쉼표를 빼고 소문자.
    @이름 은 띄어 쓸 수 없어 '전 산' 은 달력에 @전산 으로 적힘. JS calendar.js nameKey 와 같은 규칙"""
    return re.sub(r"[@#,，\s]", "", str(name or "")).lower()


def _head(plain):
    """머리 칸(기호 뺀 날짜 칸) → (open_, 시작 글자, 끝 글자, 날짜 뒤 글자). 날짜로 시작하지 않으면 None"""
    if m := _OPEN_START_RE.match(plain):
        return "start", None, m[1], m[2]
    if m := _OPEN_END_RE.match(plain):
        return "end", m[1], None, m[2]
    if m := _FIRST_RE.match(plain):
        return "", m[1], m[2], m[3]
    return None


def _title_in_head(time):
    """날짜 뒤 글자가 시간이 아니면 제목 ("261119 과제 실사, ...") → 다음 칸부터 태그·장소"""
    time = time.strip()
    return bool(time) and not _TIME_RE.match(time)


def _date(text):
    if "-" in text:
        return dt.date.fromisoformat(text)
    return dt.date(2000 + int(text[:2]), int(text[2:4]), int(text[4:6]))


def _end_date(text, start):
    if len(text) == 2:      # DD
        d = start.replace(day=int(text))
    elif len(text) == 4:    # MMDD
        d = start.replace(month=int(text[:2]), day=int(text[2:]))
    else:
        d = _date(text)
    if d < start and len(text) <= 4:  # 연/월 넘어감 (예: 261230-02)
        d = d.replace(year=d.year + 1) if len(text) == 4 else _add_month(d)
    return d


def _add_month(d):
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=y, month=m)


def parse_line(line):
    """한 줄 → 일정 dict. 일정 줄이 아니면 None."""
    raw = line.strip()  # 달력에서 수정할 때 파일에서 이 줄을 찾는 데 씀
    who = _WHO_RE.search(raw)
    line = _LIST_RE.sub("", raw[:who.start()] if who else raw, count=1)
    if not line:  # 제목·인용 등 날짜로 시작하지 않는 줄은 아래 정규식에서 걸러짐
        return None
    if m := _DECO_RE.match(line):
        try:
            d = _date(m[1]).isoformat()
        except ValueError:
            return None
        return {"deco": True, "start": d, "end": d, "time": "", "text": m[2].strip(), "raw": raw,
                "by": who[1] if who else "", "mod": (who[2] or "") if who else ""}
    fields = [f.strip() for f in re.split(r"[,，]", line)]
    head = fields[0]
    holiday = "==" in head
    important = "**" in head
    italic = bool(re.search(r"(?<!\*)\*(?!\*)", head.replace("***", "*")))  # *기울임* (***굵게+기울임*** 포함)
    plain = _MARK_RE.sub("", head).strip()
    head = _head(plain)
    if not head:
        return None
    open_, s, e, time = head
    try:
        if open_ == "start":
            # 달력엔 마감 전날부터 그려서 앞쪽이 페이드로 들어오게
            end = _date(e)
            start = end - dt.timedelta(days=1)
        elif open_ == "end":
            start = _date(s)
            end = start + dt.timedelta(days=1)
        else:
            start = _date(s)
            end = _end_date(e, start) if e else start
    except ValueError:
        return None
    if end < start:
        start, end = end, start
    time = time.strip()
    if _title_in_head(time):
        # 날짜 뒤가 시간이 아니면 제목으로 본다 ("261119 과제 실사, ...")
        fields.insert(1, time)
        time = ""
    rest = [_MARK_RE.sub("", f).strip() for f in fields[1:]]
    title = rest[0] if rest else ""
    tags, members, member_ids, others = [], [], [], []
    for f in rest[1:]:
        if f[:1] in ("#", "@"):
            for t in f.split():
                if len(t) > 1 and t[0] == "#":
                    tags.append(t[1:])
                elif len(t) > 1 and t[0] == "@":
                    m = _MEM_RE.match(t)
                    name, mid = (m[1], m[2]) if m else (t[1:], "")
                    if name not in members:
                        members.append(name)
                        member_ids.append(mid)
        else:
            others.append(f)  # 빈 칸도 자리로 셈: "제목, , 메모" = 장소 없이 메모
    while others and not others[-1]:
        others.pop()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "time": re.sub(r"\s*[-~]\s*", "-", time).rstrip("?"),
        "uncertain": time.endswith("?"),  # 10:00-10:30? = 그 사이 언젠가
        "open": open_,  # "start" = 시작 미정(-261119), "end" = 끝 미정(261119-)
        "title": title,
        "tags": tags,
        "members": members,  # @이름 (팀스페이스는 team.with_authors 가 id 로 현재 이름으로 바꿈)
        "member_ids": member_ids,  # members 와 같은 순서, id 없는 멤버는 ""
        "place": others[0] if others else "",
        "memo": ", ".join(f for f in others[1:] if f),
        "holiday": holiday,
        "important": important,
        "italic": italic,
        "raw": raw,
        "by": who[1] if who else "",   # 작성 팀원 id (꼬리표)
        "mod": (who[2] or "") if who else "",   # 마지막 수정 팀원 id
    }


def _tag_member_ids(body, ids):
    """태그 칸(제목 다음의 #·@ 로 시작하는 칸)의 id 없는 @이름 중 팀원 이름(name_key)과 같은 것에 ~id 를 붙임.
    제목 칸은 '@홍길동 면담' 처럼 @ 로 시작해도 글자 그대로 둠"""
    def field(f):
        if _MARK_RE.sub("", f).strip()[:1] not in ("#", "@"):
            return f
        return re.sub(r"(?<!\S)@([^\s~]+)(?!\S)",
                      lambda m: f"@{m[1]}~{ids[name_key(m[1])]}" if name_key(m[1]) in ids else m[0], f)
    parts = re.split(r"([,，])", body)   # [첫 칸, 구분자, 칸, 구분자, 칸 ...]
    head = _head(_MARK_RE.sub("", _LIST_RE.sub("", parts[0].strip(), count=1)).strip())
    first = 2 if head and _title_in_head(head[3]) else 4   # 첫 태그 칸 자리 (제목이 머리 칸에 있으면 바로 다음 칸)
    return "".join(field(x) if j >= first and j % 2 == 0 else x for j, x in enumerate(parts))


def stamp(old, new, mid, ids=None):
    """팀스페이스 일정 파일 저장 전: old 에 글자 그대로 없는 일정 줄에 작성자 꼬리표를 붙임.
    꼬리표 없는 줄 = 새로 씀 → " ~mid", 꼬리표 있는 줄 = 고침(달력 수정이 원래 꼬리표를 넘김) → " ~작성id>mid".
    그대로이거나 옮기기만 한 줄은 안 건드림. 지운 줄은 꼬리표도 같이 사라짐.
    ids = {name_key(이름): 팀원id} 이면 바뀐 줄의 @이름 에 ~id 를 붙임"""
    keep = {l.strip() for l in (old or "").splitlines()}
    out = []
    for line in new.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.strip() and body.strip() not in keep and parse_line(body):
            who = _WHO_RE.search(body)
            if ids:
                cut = who.start() if who else len(body)
                body = _tag_member_ids(body[:cut], ids) + body[cut:]
                who = _WHO_RE.search(body)
            if who:
                body = body[:who.start()] + (f" ~{who[1]}" if who[1] == mid else f" ~{who[1]}>{mid}")
            else:
                body = body.rstrip() + f" ~{mid}"
        out.append(body + line[len(line.rstrip("\r\n")):])
    return "".join(out)


def sort_key(ev):
    """날짜순, 같은 날은 시간 없는 것(기간·종일) 먼저, 그다음 시간순 (9:30 < 15:00)"""
    t = ev["time"].split("-")[0]
    return ev["start"], t.zfill(5) if t else ""


def parse(text):
    events = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        ev = parse_line(line)
        if ev:
            events.append(ev)
    events.sort(key=sort_key)
    return events
