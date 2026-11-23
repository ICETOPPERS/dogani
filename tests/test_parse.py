"""파서 시험: 일정 줄(schedule.py)·노트 할일·파일 이름(server.py)·공휴일 받은 JSON(holidays.py).

Python↔JS 가 같아야 하는 줄은 fixtures/shared.json 에 두고 shared.test.mjs 도 같은 파일을 읽음 (한쪽만 고치면 다른 쪽 시험이 깨짐)
"""
import datetime as dt
import json
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import holidays  # noqa: E402
import schedule  # noqa: E402
import server  # noqa: E402
import team  # noqa: E402

with open(os.path.join(HERE, "fixtures", "shared.json"), encoding="utf-8") as f:
    SHARED = json.load(f)

P = schedule.parse_line


class SharedTest(unittest.TestCase):
    """JS 와 같은 정답 파일 (schedLine 이 쓴 줄을 파이썬이 같은 뜻으로 읽는지, 정규식이 같은 줄을 고르는지)"""

    def test_sched_lines(self):
        for c in SHARED["sched"]:
            with self.subTest(c["line"]):
                ev = P(c["line"])
                self.assertIsNotNone(ev)
                self.assertEqual({k: ev[k] for k in c["ev"]}, c["ev"])

    def test_name_key(self):
        """@이름 비교 규칙: 팀원 이름 겹침(team)·~id 붙이기(stamp)·달력 칩(JS nameKey)이 같은 답"""
        for c in SHARED["names"]:
            self.assertEqual(schedule.name_key(c["in"]), c["key"], c["in"])

    def test_regex_lines(self):
        for c in SHARED["lines"]:
            with self.subTest(c["text"]):
                t = c["text"]
                m = server.TASK_RE.match(t)
                self.assertEqual(m[1] if m else None, c["task"])
                m = schedule._LIST_RE.match(t)
                self.assertEqual(m[0] if m else "", c["list"])
                self.assertEqual(bool(schedule._WHO_RE.search(t)), c["who"])
                if "dates" in c:
                    self.assertEqual(dict(server.TASK_DATE_RE.findall(t)), c["dates"])


class ParseLineTest(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(P("2026-11-20, 점검")["start"], "2026-11-20")
        self.assertEqual(P("261128-30, 기간")["end"], "2026-11-30")            # 끝 = DD
        self.assertEqual(P("261230-02, 해넘김")["end"], "2027-01-02")          # DD 가 작으면 다음 달
        self.assertEqual(P("261230-0102, 해넘김")["end"], "2027-01-02")        # MMDD 가 작으면 다음 해
        self.assertEqual(P("261120~261122, 물결")["end"], "2026-11-22")
        ev = P("261125-261120, 거꾸로")
        self.assertEqual((ev["start"], ev["end"]), ("2026-11-20", "2026-11-25"))

    def test_not_schedule(self):
        for line in ("", "# 팀 일정", "- [ ] 할일", "> 인용", "메모 261120", "261332, 없는 날짜", "260230, 2월 30일",
                     "-261340, 마감 틀림"):
            with self.subTest(line):
                self.assertIsNone(P(line))

    def test_title_and_fields(self):
        self.assertEqual(P("261119 과제 실사, #과제")["title"], "과제 실사")    # 날짜 뒤가 시간이 아니면 제목
        ev = P("261120，점검，#전산，본관")                                    # 전각 쉼표
        self.assertEqual((ev["title"], ev["tags"], ev["place"]), ("점검", ["전산"], "본관"))
        ev = P("- 261120, 점검, @가 @가 #전산 @나, 본관, 메모1, 메모2")        # 목록 기호, 같은 멤버 두 번
        self.assertEqual(ev["members"], ["가", "나"])
        self.assertEqual(ev["memo"], "메모1, 메모2")
        self.assertEqual(P("261120, 점검, , , ")["place"], "")
        self.assertEqual(P("261120 10:00~10:30?, 언젠가")["time"], "10:00-10:30")

    def test_member_id_and_author(self):
        ev = P("261120, 면담, @홍길동~1a2b3c4d @김 ~deadbeef>0badf00d")
        self.assertEqual(ev["members"], ["홍길동", "김"])
        self.assertEqual(ev["member_ids"], ["1a2b3c4d", ""])
        self.assertEqual((ev["by"], ev["mod"]), ("deadbeef", "0badf00d"))
        self.assertEqual(ev["raw"], "261120, 면담, @홍길동~1a2b3c4d @김 ~deadbeef>0badf00d")

    def test_deco(self):
        ev = P("261120 ✦ 꾸밈 글자 ~1a2b3c4d")
        self.assertTrue(ev["deco"])
        self.assertEqual((ev["start"], ev["text"], ev["by"]), ("2026-11-20", "꾸밈 글자", "1a2b3c4d"))
        self.assertEqual(P("261120 ✦")["text"], "")    # 글자가 비어도 장식 (일정 "✦" 로 둔갑하지 않음)
        self.assertIsNone(P("261340 ✦ 틀린 날짜"))

    def test_parse_fence_and_order(self):
        text = ("# 일정\n261121 15:00, 오후\n```\n261121, 코드 안\n```\n261121 9:30, 오전\n261121, 종일\n"
                "~~~\n261120, 물결 울타리\n~~~\n261120, 전날\n")
        self.assertEqual([e["title"] for e in schedule.parse(text)], ["전날", "종일", "오전", "오후"])

    def test_schedule_file_name(self):
        for name, ok in (("2026일정.md", True), ("일정.md", True), ("2026 일정.MD", True), ("팀일정.md", False),
                         ("2026일정.md.bak", False)):
            self.assertEqual(schedule.is_schedule_file(name), ok, name)


class StampTest(unittest.TestCase):
    """팀스페이스 저장 전 작성자 꼬리표: 새 줄 ~mid, 고친 줄 ~작성id>mid, 그대로·옮긴 줄은 그대로"""
    OLD = "# 팀 일정\n261120, 그대로 ~aaaaaaaa\n261121, 옮김\n261122, 고침 ~aaaaaaaa\n"

    def test_rules(self):
        new = ("# 팀 일정 고침\n261121, 옮김\n261120, 그대로 ~aaaaaaaa\n261122, 고친 뒤 ~aaaaaaaa\n"
               "261123, 새 줄\n메모 줄\n")
        out = schedule.stamp(self.OLD, new, "bbbbbbbb").splitlines()
        self.assertEqual(out, ["# 팀 일정 고침", "261121, 옮김", "261120, 그대로 ~aaaaaaaa",
                               "261122, 고친 뒤 ~aaaaaaaa>bbbbbbbb", "261123, 새 줄 ~bbbbbbbb", "메모 줄"])

    def test_own_edit_and_retouch(self):
        """제 줄을 고치면 ~mid 하나만, 남이 고친 줄(~a>b)을 또 고치면 마지막 수정자만 바뀜"""
        st = schedule.stamp
        self.assertEqual(st("", "261120, 고침 ~bbbbbbbb\n", "bbbbbbbb"), "261120, 고침 ~bbbbbbbb\n")
        self.assertEqual(st("", "261120, 또 ~aaaaaaaa>cccccccc\n", "bbbbbbbb"), "261120, 또 ~aaaaaaaa>bbbbbbbb\n")
        self.assertEqual(st(None, "261120, 새 줄   ", "bbbbbbbb"), "261120, 새 줄 ~bbbbbbbb")

    def test_crlf_kept(self):
        self.assertEqual(schedule.stamp("", "261120, 가\r\n261121, 나", "bbbbbbbb"),
                         "261120, 가 ~bbbbbbbb\r\n261121, 나 ~bbbbbbbb")

    def test_member_ids(self):
        """바뀐 줄의 태그 칸 @이름 에만 ~id (대소문자 무시), 이미 id 가 있으면 그대로, 제목·장소의 @ 는 안 건드림"""
        ids = {"홍길동": "1a2b3c4d", "kim": "0badf00d"}
        out = schedule.stamp("", "261120, 회의 @홍길동, #전산 @홍길동 @Kim @남, 본관 @홍길동\n"
                                 "261121, 면담, @홍길동~1a2b3c4d\n", "bbbbbbbb", ids)
        self.assertEqual(out.splitlines(), [
            "261120, 회의 @홍길동, #전산 @홍길동~1a2b3c4d @Kim~0badf00d @남, 본관 @홍길동 ~bbbbbbbb",
            "261121, 면담, @홍길동~1a2b3c4d ~bbbbbbbb"])
        ev = P(out.splitlines()[0])
        self.assertEqual(ev["member_ids"], ["1a2b3c4d", "0badf00d", ""])

    def test_tag_member_ids_fields(self):
        f = schedule._tag_member_ids
        ids = {"홍길동": "1a2b3c4d"}
        self.assertEqual(f("**261120**, 제목, @홍길동", ids), "**261120**, 제목, @홍길동~1a2b3c4d")
        self.assertEqual(f("261120, 제목, ==@홍길동==", ids), "261120, 제목, ==@홍길동==")   # 표시 기호 붙은 이름은 그대로
        self.assertEqual(f("261120，제목，@홍길동", ids), "261120，제목，@홍길동~1a2b3c4d")
        # 제목 칸은 @ 로 시작해도 그대로 (날짜 뒤에 바로 제목이 오면 그다음 칸부터 태그 칸)
        self.assertEqual(f("261120, @홍길동 면담", ids), "261120, @홍길동 면담")
        self.assertEqual(f("261120 10:00, @홍길동 면담, @홍길동", ids), "261120 10:00, @홍길동 면담, @홍길동~1a2b3c4d")
        self.assertEqual(f("261120 면담, @홍길동", ids), "261120 면담, @홍길동~1a2b3c4d")
        self.assertEqual(f("- [ ] 261120, @홍길동", ids), "- [ ] 261120, @홍길동")

    def test_member_ids_spaced_name(self):
        """띄어 쓴 팀원 이름·대소문자 다른 이름도 달력에 적히는 꼴(@전산·@kim)로 찾아 ~id"""
        host = team.TeamHost.__new__(team.TeamHost)
        host.lock = threading.Lock()
        host.data = {"members": {"1a2b3c4d": {"name": "전 산"}, "0badf00d": {"name": "Kim"}},
                     "groups": {"9e9e9e9e": {"name": "IT 팀"}}}
        ids = host.member_ids()
        out = schedule.stamp("", "261120, 회의, @전산 @kim @IT팀\n", "bbbbbbbb", ids)
        self.assertEqual(out, "261120, 회의, @전산~1a2b3c4d @kim~0badf00d @IT팀~9e9e9e9e ~bbbbbbbb\n")


class NoteTest(unittest.TestCase):
    def test_note_date(self):
        nd = server.parse_note_date
        self.assertEqual(nd("261030 금.md"), dt.date(2026, 10, 30))
        self.assertEqual(nd("261030_회의.MD"), dt.date(2026, 10, 30))
        self.assertEqual(nd("2026-10-30.md"), dt.date(2026, 10, 30))
        for bad in ("261030금.md", "261332.md", "2026-02-30.md", "2026일정.md", "261030.txt", "x261030.md"):
            self.assertIsNone(nd(bad), bad)

    def test_count_tasks(self):
        text = "- [ ] 가\n  - [x] 하위\n* [/] 진행\n1. [-] 취소\n- 그냥\n```\n- [ ] 코드 안\n```\n-[ ] 붙음\n"
        self.assertEqual(server.count_tasks(text), (3, 4))   # 공백이 아닌 상태 문자는 모두 완료
        self.assertEqual(server.count_tasks(""), (0, 0))

    def test_dated_tasks(self):
        text = ("- [ ] **보고** [[링크]] 🛫 2026-11-25 📅 2026-11-20\n"   # 거꾸로 = 바꿈
                "- [x] 제출 📅 2026-11-21 ✅ 2026-11-20\r\n"
                "- [x] 완료 날짜 틀림 📅 2026-11-22 ✅ 2026-13-01\n"
                "- [ ] 완료 표시만 📅 2026-11-23 ✅ 2026-11-01\n"           # 체크 안 된 줄의 ✅ 는 없는 셈
                "- [ ] 📅 2026-11-24\n"
                "- [ ] 틀린 날짜 📅 2026-02-30\n"
                "- [ ] 날짜 없음\n"
                "```\n- [ ] 코드 📅 2026-11-25\n```\n")
        out = server.dated_tasks(text)
        self.assertEqual([t["line"] for t in out], [0, 1, 2, 3, 4])
        a, b, c, d, e = out
        self.assertEqual((a["start"], a["end"], a["title"], a["done"]), ("2026-11-20", "2026-11-25", "보고 링크", False))
        self.assertEqual((b["done"], b["done_on"], b["raw"], b["title"]), (True, "2026-11-20", "- [x] 제출 📅 2026-11-21 ✅ 2026-11-20", "제출"))
        self.assertIsNone(c["done_on"])
        self.assertIsNone(d["done_on"])
        self.assertEqual(e["title"], "(제목 없음)")


class HolidayCleanTest(unittest.TestCase):
    def test_clean(self):
        data = {"2026-01-01": ["1월 1일"], "2026-03-02": "대체공휴일(3ㆍ1절)", "2025-12-25": ["작년"],
                "2026-1-1": ["모양 틀림"], "2026-05-05": ["어린이날", "", "  ", "가" * 50, "b", "c", "d"],
                "2026-06-06": {"모양": "틀림"}, 7: ["숫자 키"]}
        out = holidays._clean(data, 2026)
        self.assertEqual(out["2026-01-01"], ["1월 1일"])
        self.assertEqual(out["2026-03-02"], ["대체공휴일(3ㆍ1절)"])        # 글자 하나면 목록으로
        self.assertEqual(out["2026-05-05"], ["어린이날", "가" * 40, "b", "c"])   # 빈 이름 빼고 40자·4개까지
        self.assertEqual(sorted(out), ["2026-01-01", "2026-03-02", "2026-05-05"])
        with self.assertRaises(ValueError):
            holidays._clean(["2026-01-01"], 2026)

    def test_nick(self):
        self.assertEqual(holidays._nick("대체공휴일(기독탄신일)"), "대체공휴일(성탄절)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
