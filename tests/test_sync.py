"""동기화 시나리오 시험: 실제 ShareServer 를 임시 폴더·빈 포트로 띄우고 HTTP 로 주인(이 PC)·팀원(다른 PC) 흉내.

실행 (저장소 루트에서):  "C:\\Program Files\\Python313\\python.exe" -m unittest discover -s tests -v
주인 흉내: 실제 앱처럼 주인 비밀값 헤더를 붙임. 팀원 흉내: 비밀값 없이 토큰만
"""
import json
import os
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
import space  # noqa: E402
import team  # noqa: E402
from server import CLIENT_KEY, KEY_HEADER, OWNER_HEADER, TOKEN_HEADER, ShareServer  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base, sid, token=None, secret=""):
        self.base, self.sid, self.token, self.secret = base, sid, token, secret

    def call(self, path, body=None, raw=False):
        h = {KEY_HEADER: CLIENT_KEY, "Content-Type": "application/json"}
        if self.secret:
            h[OWNER_HEADER] = self.secret
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
        self.fresh_default = self.host.default_perm
        self.host.set_default_perm("write")   # 다른 시험은 팀원이 쓸 수 있는 예전 기본값으로
        self.share = ShareServer(free_port(), "주인")
        self.share.add_space(self.host)
        self.share.start()
        self.base = f"http://127.0.0.1:{self.share.port}"
        self.owner = Client(self.base, self.host.space_id, secret=self.share.owner_secret)
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

    def test_tasks_owner_only(self):
        """노트의 날짜 붙은 할일은 주인 달력에만, 팀원 /schedule 의 tasks 는 빈 목록"""
        st, _ = self.owner.call("/note", {"date": "2026-11-20", "content": "- [ ] 보고서 📅 2026-11-25\n",
                                          "base_mtime": None})
        self.assertEqual(st, 200)
        self.assertEqual([t["title"] for t in self.owner.call("/schedule")[1]["tasks"]], ["보고서"])
        self.assertEqual(self.guest.call("/schedule")[1]["tasks"], [])

    def test_no_token_rejected(self):
        st, _ = Client(self.base, self.host.space_id, token="").call("/sched-doc")
        self.assertEqual(st, 401)

    # ------------------------------------------------------------ 주인 판정 (H1)
    def test_loopback_without_secret_not_owner(self):
        """같은 PC(루프백)여도 비밀값이 없거나 틀리면 주인이 아님"""
        self.assertEqual(Client(self.base, self.host.space_id).call("/sched-doc")[0], 401)
        self.assertEqual(Client(self.base, self.host.space_id, secret="x" * 43).call("/sched-doc")[0], 401)
        self.assertEqual(self.owner.call("/sched-doc")[0], 200)

    def test_lan_ip_never_owner(self):
        """켤 때의 LAN IP 에서 온 요청은 비밀값이 맞아도 주인이 아님 (비밀값은 127.0.0.1 로만 감)"""
        s = self.share
        s.ip = "192.0.2.7"   # 망이 없는 PC 에선 local_ip() 가 127.0.0.1 이라 LAN IP 를 직접 정함
        self.assertFalse(s.is_owner(s.ip, s.owner_secret))
        self.assertFalse(s.is_owner("::ffff:127.0.0.1", ""))
        self.assertTrue(s.is_owner("127.0.0.1", s.owner_secret))

    def test_join_own_space_refused(self):
        """내 서버 id 를 보낸 참여(내 LAN 주소로 내 초대장)·주인 비밀값을 단 참여는 거부, 초대장은 남음"""
        code = self.host.create_invite()
        guest = Client(self.base, "", token="")
        st, res = guest.call("/api/join", {"code": code, "name": "나", "instance": self.share.instance_id}, raw=True)
        self.assertEqual((st, res.get("error")), (400, "own_space"))
        st, res = Client(self.base, "", secret=self.share.owner_secret).call(
            "/api/join", {"code": code, "name": "나"}, raw=True)
        self.assertEqual((st, res.get("error")), (400, "own_space"))
        self.assertTrue(self.host.has_invite(code))
        st, res = guest.call("/api/join", {"code": code, "name": "남", "instance": "0" * 16}, raw=True)
        self.assertEqual(st, 200, res)

    def test_join_gate_locks(self):
        """틀린 초대장 FAIL_LIMIT 번 → 맞는 코드도 잠김 (제한은 JoinGate 한 곳)"""
        guest = Client(self.base, "", token="")
        for _ in range(team.FAIL_LIMIT):
            self.assertEqual(guest.call("/api/join", {"code": "AAAA-AAAA-AAAA", "name": "x"}, raw=True)[0], 401)
        code = self.host.create_invite()
        self.assertEqual(guest.call("/api/join", {"code": code, "name": "y"}, raw=True)[0], 429)

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

    # ------------------------------------------------------------ 그룹
    def test_group_perm(self):
        """그룹 권한: 개인 지정이 없으면 그룹을 따르고(하나라도 write 면 write), 개인 지정이 우선"""
        rd, it = self.host.create_group("R&D", "read"), self.host.create_group("전산")
        self.assertTrue(self.host.can_write(self.guest_id))           # 그룹 없음 = write
        self.host.set_member_group(self.guest_id, rd, True)
        self.assertFalse(self.host.can_write(self.guest_id))          # read 그룹만
        d = self.guest.doc()
        self.assertEqual(self.guest.write(d["content"] + "x\n", d["mtime"])[0], 403)
        self.host.set_member_group(self.guest_id, it, True)
        self.assertTrue(self.host.can_write(self.guest_id))           # write 그룹도 있음
        self.host.set_perm(self.guest_id, "read")
        self.assertFalse(self.host.can_write(self.guest_id))          # 개인 지정 우선
        self.host.set_perm(self.guest_id, "auto")
        self.host.delete_group(it)
        self.assertFalse(self.host.can_write(self.guest_id))
        self.assertEqual(self.host.groups()[0]["members"], [self.guest_id])
        self.assertTrue(self.host.can_write(self.host.owner_id))      # 관리자는 항상

    def test_group_cards(self):
        """사이드바 그룹 칩: /schedule 의 groups 에 구성원 이름과 실제 권한(개인 지정 반영)"""
        rd = self.host.create_group("R&D", "read")
        self.host.set_member_group(self.guest_id, rd, True)
        self.host.set_perm(self.guest_id, "write")   # 개인 지정이 그룹보다 우선 → 미리보기도 편집
        gs = self.guest.call("/schedule")[1]["groups"]
        self.assertEqual(gs[0]["name"], "R&D")
        self.assertEqual([(m["perm"]) for m in gs[0]["members"]], ["write"])

    def test_default_perm(self):
        """새 스페이스는 기본 보기만, 예전 team.json(키 없음)은 write. 그룹·개인 지정이 기본보다 우선"""
        self.assertEqual(self.fresh_default, "read")
        self.host.set_default_perm("read")
        self.assertFalse(self.host.can_write(self.guest_id))
        self.assertEqual(self.host.data["groups"][self.host.create_group("태그용")]["perm"], "read")   # 새 그룹 = 기본 권한
        it = self.host.create_group("전산", "write")
        self.host.set_member_group(self.guest_id, it, True)
        self.assertTrue(self.host.can_write(self.guest_id))
        self.host.set_member_group(self.guest_id, it, False)
        self.host.set_default_perm("write")
        self.assertTrue(self.host.can_write(self.guest_id))
        self.host.data.pop("default_perm")   # 예전 파일 흉내
        self.assertEqual(self.host.default_perm, "write")

    def test_group_names(self):
        """그룹 이름은 팀원 이름·all·다른 그룹과 겹치지 않고, 팀원도 그룹 이름으로 못 바꿈"""
        gid = self.host.create_group("R&D")
        for bad in ("동료", "ALL", "r&d", "R& D", "", "a@b"):
            with self.assertRaises(team.TeamError):
                self.host.create_group(bad)
        self.host.rename_group(gid, "R&D")   # 제 이름 그대로는 됨
        with self.assertRaises(team.TeamError):
            self.host.update_profile(self.guest_id, "r&d")

    def test_group_mention(self):
        """@그룹 은 ~id 가 붙어 이름을 바꿔도 따라가고, @그룹·@all 은 reach 로 팀원 이름까지 펼침"""
        gid = self.host.create_group("전산")
        self.host.set_member_group(self.guest_id, gid, True)
        d = self.guest.doc()
        self.assertEqual(self.guest.write(d["content"] + "==261120==, 교육, @전산\n==261121==, 휴무, @all\n"
                                          "==261122==, 반차, @주인\n", d["mtime"])[0], 200)
        self.assertIn("@전산~" + gid, self.owner.doc()["content"])
        self.host.rename_group(gid, "IT")
        sch = self.guest.call("/schedule")[1]
        evs = {e["title"]: e for e in sch["decos"] + sch["events"]}
        self.assertEqual(evs["교육"]["members"], ["IT"])
        self.assertEqual(evs["교육"]["reach"], ["동료"])
        self.assertEqual(sorted(evs["휴무"]["reach"]), ["동료", "주인"])
        self.assertNotIn("reach", evs["반차"])
        self.assertIn("IT", sch["members"])
        self.assertIn("all", sch["members"])

    def test_reach_edge(self):
        """지운 그룹 id 는 같은 이름 새 그룹으로, 'All' 이라는 예전 팀원 id 는 그 사람만"""
        gid = self.host.create_group("전산")
        self.host.set_member_group(self.guest_id, gid, True)
        with self.host.lock:
            self.assertEqual(self.host._reach(["전산"], ["deadbeef"]), ["동료"])
            self.host.data["members"][self.guest_id]["name"] = "All"
            self.assertIsNone(self.host._reach(["All"], [self.guest_id]))

    def test_kicked_guest(self):
        self.host.kick(self.guest_id)
        self.assertEqual(self.guest.call("/sched-doc")[0], 401)

    # ------------------------------------------------------------ 알림 (SSE)
    def test_sse_schedule_event(self):
        """주인이 저장하면 팀원 연결에 schedule 이벤트가 감"""
        h = {KEY_HEADER: CLIENT_KEY, TOKEN_HEADER: self.guest.token}
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


class BrokenFileTest(unittest.TestCase):
    """깨진 설정·팀 파일은 기본값으로 덮어쓰기 전에 .broken-<시각> 으로 백업 (H2)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        space.BROKEN.clear()

    def tearDown(self):
        space.BROKEN.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def backups(self, path):
        d, name = os.path.split(path)
        return [n for n in os.listdir(d) if n.startswith(name + ".broken-")]

    def test_load_json(self):
        ok = self.write("ok.json", '{"a": 1}')
        self.assertEqual(space.load_json(ok), {"a": 1})
        self.assertIsNone(space.load_json(os.path.join(self.tmp, "none.json")))
        for name, text in (("bad.json", '{"a": '), ("list.json", "[1, 2]")):
            path = self.write(name, text)
            self.assertIsNone(space.load_json(path))
            self.assertFalse(os.path.exists(path))
            self.assertEqual(len(self.backups(path)), 1)
        self.assertEqual([os.path.basename(p) for p, _ in space.BROKEN], ["bad.json", "list.json"])
        again = self.write("bad.json", "{")   # 같은 초에 또 깨져도 먼저 백업한 원본을 덮지 않음
        space.load_json(again)
        self.assertEqual(len(self.backups(again)), 2)

    def test_broken_space_and_team(self):
        root, keys = os.path.join(self.tmp, "space"), os.path.join(self.tmp, "keys")
        os.makedirs(keys)
        sp = self.write("space/.dogani/space.json", "{broken")
        tj = self.write("space/.dogani/team.json", "{broken")
        host = team.TeamHost(root, keys, "주인")
        self.assertEqual(len(self.backups(sp)), 1)
        self.assertEqual(len(self.backups(tj)), 1)
        self.assertEqual(space.read_json(sp)["id"], host.space_id)   # 새 파일은 정상으로 다시 씀

    def test_broken_space_keeps_known_id(self):
        """space.json 이 깨져도 PC 설정에 적힌 id 로 되살림 → 서명 키(키 파일)·팀원 토큰이 그대로"""
        root, keys = os.path.join(self.tmp, "space"), os.path.join(self.tmp, "keys")
        os.makedirs(root)
        os.makedirs(keys)
        host = team.TeamHost(root, keys, "주인")
        sid, pub = host.space_id, host.pub
        sp = self.write("space/.dogani/space.json", "{broken")
        again = team.TeamHost(root, keys, "주인", known_id=sid)
        self.assertEqual((again.space_id, again.pub), (sid, pub))
        self.assertEqual(space.read_json(sp)["id"], sid)
        self.write("space/.dogani/space.json", "{broken")
        self.assertNotEqual(team.TeamHost(root, keys, "주인", known_id="../x").space_id, sid)   # 이상한 id 는 안 씀


if __name__ == "__main__":
    unittest.main(verbosity=2)
