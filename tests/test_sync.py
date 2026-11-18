"""동기화 시나리오 시험: 실제 ShareServer 를 임시 폴더·빈 포트로 띄우고 HTTP 로 주인(이 PC)·팀원(다른 PC) 흉내.

실행 (저장소 루트에서):  "C:\\Program Files\\Python313\\python.exe" -m unittest discover -s tests -v
팀원 흉내: 같은 PC 요청은 모두 주인이라, 시험용 헤더 X-Test-Guest 가 있으면 주인이 아닌 것으로 봄 (이 파일 안에서만 바꿈)
"""
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import server  # noqa: E402
import team  # noqa: E402
from server import CLIENT_KEY, KEY_HEADER, TOKEN_HEADER, Handler, ShareServer  # noqa: E402

GUEST = "X-Test-Guest"
_orig_check = Handler._check


def _check(self):
    ctx = _orig_check(self)
    if ctx and self.headers.get(GUEST):
        ctx.owner = False
    return ctx


Handler._check = _check


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base, sid, token=None):
        self.base, self.sid, self.token = base, sid, token

    def call(self, path, body=None, raw=False):
        h = {KEY_HEADER: CLIENT_KEY, "Content-Type": "application/json"}
        if self.token is not None:
            h[GUEST] = "1"
            if self.token:
                h[TOKEN_HEADER] = self.token
        url = f"{self.base}/api/s/{self.sid}{path}" if not raw else self.base + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def doc(self):
        return self.call("/sched-doc")[1]

    def write(self, content, base_mtime):
        return self.call("/sched-doc", {"content": content, "base_mtime": base_mtime})


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        root, keys = os.path.join(self.tmp, "space"), os.path.join(self.tmp, "keys")
        os.makedirs(root)
        os.makedirs(keys)
        self.host = team.TeamHost(root, keys, "주인")
        self.share = ShareServer(free_port(), "주인")
        self.share.add_space(self.host)
        self.share.start()
        self.base = f"http://127.0.0.1:{self.share.port}"
        self.owner = Client(self.base, self.host.space_id)
        code = self.host.create_invite()
        st, res = Client(self.base, "", token="").call("/api/join", {"code": code, "name": "동료"}, raw=True)
        self.assertEqual(st, 200, res)
        self.guest = Client(self.base, self.host.space_id, token=res["token"])
        self.guest_id = res["id"]

    def tearDown(self):
        self.share.stop()
        time.sleep(0.05)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 기본 읽기·쓰기
    def test_owner_write_guest_reads(self):
        d = self.owner.doc()
        st, _ = self.owner.write(d["content"] + "261120, 점검, #전산\n", d["mtime"])
        self.assertEqual(st, 200)
        st, sch = self.guest.call("/schedule")
        self.assertEqual(st, 200)
        self.assertIn("점검", [e["title"] for e in sch["events"]])
        self.assertIn("동료", sch["members"])

    def test_no_token_rejected(self):
        st, _ = Client(self.base, self.host.space_id, token="").call("/sched-doc")
        self.assertEqual(st, 401)

    # ------------------------------------------------------------ 충돌
    def test_stale_write_conflict(self):
        """둘이 같은 판을 받아 각자 저장 → 늦은 쪽은 409 (덮어쓰지 않음)"""
        a, b = self.owner.doc(), self.guest.doc()
        self.assertEqual(self.owner.write(a["content"] + "261121, 가\n", a["mtime"])[0], 200)
        time.sleep(0.01)
        self.assertEqual(self.guest.write(b["content"] + "261122, 나\n", b["mtime"])[0], 409)
        self.assertIn("가", self.owner.doc()["content"])

    def test_concurrent_writes_one_wins(self):
        """같은 판으로 동시에 저장 8번 → 정확히 1번만 통과 (확인~쓰기 잠금)"""
        d = self.owner.doc()
        results = []
        def go(i):
            c = self.guest if i % 2 else self.owner
            results.append(c.write(d["content"] + f"2611{10 + i}, 동시{i}\n", d["mtime"])[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), [200] + [409] * 7)

    def test_new_file_both_create(self):
        """base_mtime null = 새 파일로 알고 씀. 그 사이 생겼으면 409"""
        os.remove(self.host.store.sched_doc_path())
        self.assertEqual(self.owner.write("261120, 하나\n", None)[0], 200)
        self.assertEqual(self.guest.write("261120, 둘\n", None)[0], 409)

    # ------------------------------------------------------------ 권한·작성자
    def test_guest_stamp(self):
        """팀원이 쓴 줄 끝에 작성자 꼬리표 ~id, 화면용 일정에는 꼬리표가 없음"""
        d = self.guest.doc()
        self.assertEqual(self.guest.write(d["content"] + "261123, 팀원 일정\n", d["mtime"])[0], 200)
        self.assertRegex(self.owner.doc()["content"], r"261123, 팀원 일정 ~" + self.guest_id)
        ev = next(e for e in self.guest.call("/schedule")[1]["events"] if e["title"] == "팀원 일정")
        self.assertNotIn("~", ev["title"])

    def test_read_only_guest(self):
        self.host.set_perm(self.guest_id, "read")
        d = self.guest.doc()
        self.assertEqual(self.guest.write(d["content"] + "x\n", d["mtime"])[0], 403)
        self.assertFalse(self.guest.call("/schedule")[1]["writable"])

    def test_kicked_guest(self):
        self.host.kick(self.guest_id)
        self.assertEqual(self.guest.call("/sched-doc")[0], 401)

    # ------------------------------------------------------------ 알림 (SSE)
    def test_sse_schedule_event(self):
        """주인이 저장하면 팀원 연결에 schedule 이벤트가 감"""
        h = {KEY_HEADER: CLIENT_KEY, GUEST: "1", TOKEN_HEADER: self.guest.token}
        req = urllib.request.Request(f"{self.base}/api/s/{self.host.space_id}/events", headers=h)
        got = threading.Event()
        def listen():
            with urllib.request.urlopen(req, timeout=5) as r:
                for line in r:
                    if line.strip() == b"event: schedule":
                        got.set()
                        return
        threading.Thread(target=listen, daemon=True).start()
        time.sleep(0.3)
        d = self.owner.doc()
        self.owner.write(d["content"] + "261124, 알림\n", d["mtime"])
        self.assertTrue(got.wait(3), "schedule 이벤트를 받지 못함")


class TagRenameTest(unittest.TestCase):
    """app.js 태그 이름 일괄 변경과 같은 규칙 (JS 정규식을 그대로 옮김: 바꾸면 같이 고칠 것)"""

    @staticmethod
    def rename(text, old, new):
        return re.sub(r"(^|[\s,，])#" + re.escape(old) + r"(?=[\s,，]|$)", lambda m: m[1] + "#" + new, text, flags=re.M)

    def test_rules(self):
        src = "# 팀 일정\n261120, 점검, #전산 #외근\n261121, 교육, #전산팀\n261122,가,#전산,장소\n- [ ] 할일 #전산\n"
        out = self.rename(src, "전산", "IT")
        self.assertIn("#IT #외근", out)
        self.assertIn("#전산팀", out)          # 앞부분만 같은 태그는 그대로
        self.assertIn(",#IT,장소", out)
        self.assertIn("할일 #IT", out)
        self.assertIn("# 팀 일정", out)         # 제목 줄은 그대로

    def test_parse_after_rename(self):
        import schedule
        ev = schedule.parse_line("261120, 점검, #IT #외근") if hasattr(schedule, "parse_line") else None
        if ev is None:
            self.skipTest("schedule.parse_line 없음")
        self.assertEqual(ev["tags"], ["IT", "외근"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
