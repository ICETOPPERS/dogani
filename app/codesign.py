"""배포 서명: 태그의 app/ 파일 목록(sha256)과 태그 이름을 ed25519 로 서명해 app/release.sig 로 같이 커밋한다.

- 서명하는 쪽: tools/sign_release.py (개인 키는 저장소 밖, 배포하는 PC 에만)
- 확인하는 쪽: updater.stage() 가 받은 zip 을 풀기 전에 check() → 지금 실행 중인 코드의 공개키로만 확인하므로
  GitHub 계정이나 태그가 털려도 개인 키 없이는 동료 PC 에서 코드가 실행되지 않음.
- zip 바이트가 아니라 파일 내용을 서명: GitHub zip 은 바이트가 같다는 보장이 없고 .gitattributes(*.bat eol=crlf)로
  줄바꿈이 바뀌어 나옴 → 글자 파일(TEXT_EXTS)은 해시 전에 CRLF 를 LF 로 맞춤.
- 태그 이름도 서명에 넣음: 예전 서명본을 새 태그로 다시 올려 예전 코드로 되돌리는 것을 막음.
"""
import hashlib
import importlib.util
import json
import os

SIG_NAME = "release.sig"
SKIP_DIRS = ("editor/",)   # updater 가 받지 않는 폴더 (서명에서도 뺌)
_PREFIX = b"dogani-release\n"   # 다른 용도(팀 토큰 등) 서명과 섞이지 않게
TEXT_EXTS = (".py", ".js", ".mjs", ".css", ".html", ".bat", ".txt", ".md", ".json", ".svg", ".iss")


def _load_ed25519():
    """같은 폴더 ed25519.py 를 sys.modules 에 올리지 않고 읽음. 런처가 updater 를 파일 경로로 불러
    sys.path 에 이 폴더가 없고, 이름으로 올리면 코드를 바꿔 끼운 뒤 dogani 가 예전 모듈을 그대로 씀"""
    spec = importlib.util.spec_from_file_location("_codesign_ed25519",
                                                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "ed25519.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ed25519 = _load_ed25519()


def wanted(rel):
    """서명·업데이트 대상 파일인지 (app/ 기준 상대 경로, / 구분). zip 의 폴더 항목(app/ 자체 = "")은 뺌"""
    return bool(rel) and rel != SIG_NAME and not rel.startswith(SKIP_DIRS) and not rel.endswith("/")


def digest(rel, data):
    """줄바꿈은 글자 파일만 맞춤: 바이너리(그림·글꼴)까지 바꾸면 \\r 을 넣고 빼도 서명이 그대로 맞음"""
    if rel.lower().endswith(TEXT_EXTS):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def manifest(tag, files):
    """서명할 바이트. files = {상대 경로: 내용 bytes}"""
    body = {"tag": tag, "files": {rel: digest(rel, data) for rel, data in files.items() if wanted(rel)}}
    return _PREFIX + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def make(tag, files, secret):
    """release.sig 내용 (사람이 봐도 알 수 있게 파일 목록을 같이 적음. 확인은 다시 계산한 manifest 로만)"""
    sig = ed25519.sign(secret, manifest(tag, files))
    out = {"tag": tag, "key": ed25519.public_key(secret).hex(), "sig": sig.hex(),
           "files": {rel: digest(rel, data) for rel, data in sorted(files.items()) if wanted(rel)}}
    return (json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def check(tag, files, keys):
    """files(받은 app/ 전체, release.sig 포함)가 keys 중 하나로 tag 에 서명됐는지. 맞으면 None, 아니면 이유"""
    raw = files.get(SIG_NAME)
    if raw is None:
        return "서명 파일 없음"
    try:
        info = json.loads(raw.decode("utf-8"))
        sig = bytes.fromhex(info["sig"])
        key = bytes.fromhex(info["key"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return "서명 파일이 깨짐"
    if info.get("tag") != tag:
        return f"다른 태그의 서명 ({info.get('tag')!r})"
    if key.hex() not in keys:
        return "모르는 서명 키"
    if not ed25519.verify(key, manifest(tag, files), sig):
        return "서명이 맞지 않음 (파일이 바뀌었거나 빠지거나 더해짐)"
    return None
