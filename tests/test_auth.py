"""팀 인증 시험 (서버 없이 TeamHost·JoinGate 를 직접): 초대장 만료·다시 초대·토큰·참여 시도 잠금·호스트 확인 서명 제한"""
import os
import secrets
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import team  # noqa: E402
from server import JoinGate  # noqa: E402
from team import TeamError, TeamHost  # noqa: E402


class HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dogani-test-")
        root, keys = os.path.join(self.tmp, "space"), os.path.join(self.tmp, "keys")
        os.makedirs(root)
        os.makedirs(keys)
        self.host = TeamHost(root, keys, "주인")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def join(self, code, name="동료"):
        return self.host.join("192.0.2.1", code, name)

    def error(self, fn, *args):
        with self.assertRaises(TeamError) as cm:
            fn(*args)
        return cm.exception.code, cm.exception.status


class InviteTest(HostCase):
    def test_code_shape_and_input(self):
        """XXXX-XXXX-XXXX 로 보여 주고, 소문자·띄어쓰기로 넣어도 받음. 한 번 쓰면 폐기"""
        code = self.host.create_invite()
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        res = self.join(" " + code.lower().replace("-", " ") + " ")
        self.assertEqual(self.host.member(res["token"]), res["id"])
        self.assertFalse(self.host.has_invite(code))
        self.assertEqual(self.error(self.join, code, "또"), ("bad_code", 401))

    def test_expired(self):
        code = self.host.create_invite()
        self.host.data["invites"][team.normalize_invite(code)]["exp"] = int(time.time()) - 1
        self.assertEqual(self.error(self.join, code), ("bad_code", 401))
        self.assertEqual(self.host.invites(), [])   # 만료된 것은 목록에서도 정리
        self.assertEqual(self.error(self.join, "SHORT"), ("bad_code", 401))

    def test_name_rules(self):
        """빈 이름·겹치는 이름(대소문자·띄어쓰기 무시)·그룹 이름은 거절하고 초대장은 남김"""
        self.join(self.host.create_invite(), "Kim Lee")
        code = self.host.create_invite()
        self.assertEqual(self.error(self.join, code, "   "), ("bad_name", 400))
        self.assertEqual(self.error(self.join, code, "kimlee"), ("name_taken", 409))
        self.assertEqual(self.error(self.join, code, "주인"), ("name_taken", 409))
        self.host.create_group("전산")
        self.assertEqual(self.error(self.join, code, "전산"), ("name_taken", 409))
        self.assertEqual(self.error(self.join, code, "ALL"), ("name_taken", 409))
        self.assertTrue(self.host.has_invite(code))
        self.assertEqual(self.join(code, "  새   사람  ")["name"], "새 사람")

    def test_reinvite_min_iat(self):
        """다시 초대로 새 PC 가 참여하면 예전 PC 토큰(같은 초에 만든 것 포함)은 무효, 이름은 그대로"""
        old = self.join(self.host.create_invite(), "동료")
        same_sec = team.make_token(self.host.data["sign_key"], old["id"])
        code = self.host.create_invite(old["id"])
        self.assertEqual(self.host.invites()[0]["label"], "동료 다시 초대")
        new = self.join(code, "다른 이름")
        self.assertEqual((new["id"], new["name"]), (old["id"], "동료"))
        self.assertIsNone(self.host.member(old["token"]))
        self.assertIsNone(self.host.member(same_sec))
        self.assertEqual(self.host.member(new["token"]), old["id"])

    def test_reinvite_one_alive(self):
        """다시 초대는 한 사람당 한 장, 관리자·없는 팀원은 다시 초대 못 함, 내보내면 그 초대장도 지움"""
        mid = self.join(self.host.create_invite())["id"]
        first = self.host.create_invite(mid)
        second = self.host.create_invite(mid)
        self.assertFalse(self.host.has_invite(first))
        self.assertTrue(self.host.has_invite(second))
        self.assertEqual(self.error(self.host.create_invite, self.host.owner_id), ("not_member", 404))
        self.assertEqual(self.error(self.host.create_invite, "deadbeef"), ("not_member", 404))
        self.host.kick(mid)
        self.assertFalse(self.host.has_invite(second))

    def test_reinvite_after_kick(self):
        """초대장이 남은 채 팀원이 사라졌으면(예전 파일 등) 참여를 거절하고 그 초대장을 지움"""
        mid = self.join(self.host.create_invite())["id"]
        code = self.host.create_invite(mid)
        del self.host.data["members"][mid]
        self.assertEqual(self.error(self.join, code), ("bad_code", 404))
        self.assertFalse(self.host.has_invite(code))


class TokenTest(HostCase):
    def test_bad_tokens(self):
        res = self.join(self.host.create_invite())
        tok = res["token"]
        mid, iat, sig = tok.split(".")
        os.makedirs(os.path.join(self.tmp, "space2"))
        other = TeamHost(os.path.join(self.tmp, "space2"), os.path.join(self.tmp, "keys"), "남")
        for bad in (None, "", "a.b", tok + "0", f"{mid}.{iat}.{'0' * 128}", f"{mid}.{int(iat) + 1}.{sig}",
                    f"deadbeef.{iat}.{sig}", f"{mid}.x.{sig}", team.make_token(other.data["sign_key"], mid)):
            self.assertIsNone(self.host.member(bad), bad)
        self.assertEqual(self.host.member(tok), mid)
        self.host.kick(mid)
        self.assertIsNone(self.host.member(tok))


class ProveTest(HostCase):
    def test_hello(self):
        """nonce 서명은 그 팀 공개키로만 맞고, 모양이 틀린 nonce 는 서명하지 않음"""
        nonce = secrets.token_hex(16)
        sig = self.host.prove(nonce)
        self.assertTrue(team.verify_hello(self.host.pub, nonce, sig))
        self.assertFalse(team.verify_hello(self.host.pub, secrets.token_hex(16), sig))
        self.assertFalse(team.verify_hello("", nonce, sig))
        self.assertFalse(team.verify_hello(self.host.pub, nonce, "zz"))
        for bad in ("", None, nonce[:-1], nonce.upper(), "g" * 32):
            self.assertEqual(self.host.prove(bad), "", bad)

    def test_limit(self):
        """PROVE_WINDOW 초에 PROVE_LIMIT 번까지, 지나면 다시 됨"""
        nonce = secrets.token_hex(16)
        for _ in range(team.PROVE_LIMIT):
            self.assertTrue(self.host.prove(nonce))
        self.assertEqual(self.host.prove(nonce), "")
        self.host.proofs = [t - team.PROVE_WINDOW for t in self.host.proofs]
        self.assertTrue(self.host.prove(nonce))


class JoinGateTest(unittest.TestCase):
    def test_lock_and_reset(self):
        g, ip = JoinGate(), "192.0.2.9"
        for _ in range(team.FAIL_LIMIT):
            g.check(ip)
            with self.assertRaises(TeamError):
                g.fail(ip)
        with self.assertRaises(TeamError) as cm:
            g.check(ip)
        self.assertEqual(cm.exception.status, 429)
        g.check("192.0.2.10")                          # 다른 IP 는 따로 셈
        g.fails[ip][1] = time.time() - 1               # 잠금 시간이 지남
        g.check(ip)
        with self.assertRaises(TeamError):
            g.fail(ip)
        self.assertEqual(g.fails[ip][0], 1)            # 잠금이 풀린 뒤 틀리면 처음부터 셈
        g.ok(ip)
        self.assertNotIn(ip, g.fails)


if __name__ == "__main__":
    unittest.main(verbosity=2)
