"""배포 서명 (배포하는 PC 에서만).

    python tools/sign_release.py keygen        개인 키를 만들고 공개키를 출력 (updater.SIGN_KEYS 에 넣음)
    python tools/sign_release.py v26.11.24.01  스테이징된 app/ 을 서명해 app/release.sig 를 쓰고 git add

태그 커밋 직전에 돌림: 커밋될 내용(인덱스)을 서명하므로 git add 를 다 한 뒤에.
개인 키는 저장소 밖(~/.dogani/release_key)에 두고 따로 백업. 잃어버리면 동료 PC 가 새 태그를 받지 못함
(새 키는 예전 키로 서명한 태그에 SIGN_KEYS 를 더해서만 넘길 수 있음).
"""
import ast
import os
import re
import secrets
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import codesign  # noqa: E402

KEY_PATH = os.environ.get("DOGANI_SIGN_KEY") or os.path.join(os.path.expanduser("~"), ".dogani", "release_key")
TAG_RE = re.compile(r"^v\d{2}\.\d{2}\.\d{2}\.\d{2}$")


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, check=True).stdout


def staged_app():
    """인덱스에 올라간 app/ 파일 {상대 경로: 내용}. 작업 폴더가 아니라 커밋될 내용 그대로"""
    files = {}
    for entry in git("ls-files", "-s", "-z", "--", "app").split(b"\0"):
        if not entry:
            continue
        meta, path = entry.decode("utf-8").split("\t", 1)
        if meta.split()[2] != "0":   # 합치기 충돌 중: 같은 경로가 여러 번 나오고 커밋될 내용이 아직 없음
            sys.exit(f"충돌이 풀리지 않은 파일이 있음: {path}")
        rel = path[len("app/"):]
        if codesign.wanted(rel):
            files[rel] = git("cat-file", "blob", meta.split()[1])
    return files


def sign_keys(source):
    """updater.py 원문에서 SIGN_KEYS. 없으면(서명 도입 전 버전) None"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SIGN_KEYS" for t in node.targets):
            try:
                return tuple(ast.literal_eval(node.value))
            except ValueError:
                return None
    return None


def previous_tag(tag):
    """tag 보다 앞선 가장 최신 버전 태그 = 동료 PC 가 지금 돌리는 코드 (그 SIGN_KEYS 로 확인함)"""
    names = [t for t in git("tag", "--list", "v*").decode().split() if TAG_RE.match(t) and t < tag]
    return max(names) if names else None


def trusted_by_all(pub, tag):
    """확인하는 쪽 둘 다 이 키를 믿는지: 지금 동료들(직전 태그)과, 이 태그를 받은 뒤의 동료들(인덱스).
    작업 폴더의 updater.py 는 안 봄 (커밋 안 될 수도 있고, 새 키를 같은 태그에서 바로 쓰면 아무도 못 받음)"""
    try:
        staged = sign_keys(git("show", ":app/updater.py").decode("utf-8"))
    except subprocess.CalledProcessError:
        staged = None
    if not staged or pub not in staged:
        return f"이 키({pub[:16]}…)가 스테이징된 app/updater.py 의 SIGN_KEYS 에 없음"
    prev = previous_tag(tag)
    if prev:
        try:
            old = sign_keys(git("show", f"{prev}:app/updater.py").decode("utf-8"))
        except subprocess.CalledProcessError:
            old = None
        if old is not None and pub not in old:   # None = 서명 도입 전 버전 (확인 없이 받음)
            return f"이 키({pub[:16]}…)를 직전 태그 {prev} 가 모름 → 동료 PC 가 받지 않음. 예전 키로 서명할 것"
    return None


def keygen():
    if os.path.exists(KEY_PATH):
        sys.exit(f"이미 키가 있음: {KEY_PATH}")
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    secret = secrets.token_bytes(32)
    with open(KEY_PATH, "x", encoding="ascii") as f:
        f.write(secret.hex() + "\n")
    print(f"개인 키: {KEY_PATH} (백업할 것)")
    print(f"공개키: {codesign.ed25519.public_key(secret).hex()}")


def sign(tag):
    if not TAG_RE.match(tag):
        sys.exit(f"태그 모양이 아님: {tag}")
    try:
        with open(KEY_PATH, encoding="ascii") as f:
            secret = bytes.fromhex(f.read().strip())
    except (OSError, ValueError):
        sys.exit(f"개인 키를 읽지 못함: {KEY_PATH}")
    pub = codesign.ed25519.public_key(secret).hex()
    err = trusted_by_all(pub, tag)
    if err:
        sys.exit(err)
    files = staged_app()
    data = codesign.make(tag, files, secret)
    # 확인하는 쪽과 같은 함수로 한 번 더 (서명 파일 포함 받은 그대로라 치고)
    err = codesign.check(tag, dict(files, **{codesign.SIG_NAME: data}), (pub,))
    if err:
        sys.exit(f"서명 확인 실패: {err}")
    path = os.path.join(ROOT, "app", codesign.SIG_NAME)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    git("add", "--", path)
    print(f"{tag}: 파일 {len(files)}개 서명 → app/{codesign.SIG_NAME} (git add 함)")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")   # Git Bash 로 받으면 cp949 로 깨짐
    if sys.argv[1:] == ["keygen"]:
        keygen()
    elif len(sys.argv) == 2:
        sign(sys.argv[1])
    else:
        sys.exit(__doc__)
