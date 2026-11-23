"""
스페이스 = dogani 가 여는 폴더 하나 (옵시디언 볼트와 같은 개념)

- 스페이스 설정은 그 폴더의 `.dogani/space.json` 에 둔다 → 폴더를 옮기거나 복사해도 따라감
- 개인 스페이스와 팀스페이스는 같은 형식. 팀스페이스는 `.dogani/team.json` 이 하나 더 있을 뿐 (team.py)
- 비밀값(팀 서명 키·초대장)과 PC 마다 다른 값(창 위치 등)은 스페이스에 두지 않고 %APPDATA%\\dogani 에 둔다
- 노트 스캔은 점(.)으로 시작하는 폴더를 건너뛰므로 .dogani / .obsidian 은 노트로 읽지 않음
"""
import datetime as dt
import json
import os
import re
import secrets

META_DIR = ".dogani"
SPACE_FILE = "space.json"


def meta_dir(root):
    return os.path.join(root, META_DIR)


def read_json(path):
    """JSON 객체 파일 읽기 → dict. 없거나 깨졌거나 객체가 아니면 None"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


BROKEN = []   # 이번 실행에 깨져서 백업한 파일 [(원래 경로, 백업 경로)] → 앱이 트레이 알림으로 알리고 비움


def load_json(path):
    """설정·팀 파일 읽기 → dict 또는 None. read_json 과 같되, 파일은 있는데 JSON 이 깨졌으면
    .broken-<시각> 으로 옮겨 둔다. 기본값으로 덮어쓰기 전에 원본을 남겨 손으로 되살릴 수 있게."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:   # 없거나 잠깐 못 읽음 → 깨진 것이 아니므로 옮기지 않음
        return None
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    stamp = f"{path}.broken-{dt.datetime.now():%Y%m%d-%H%M%S}"
    for n in range(1, 100):   # 같은 초에 또 깨져도 먼저 남긴 백업(진짜 원본)을 덮지 않게 rename (있으면 실패)
        backup = stamp if n == 1 else f"{stamp}-{n}"
        try:
            os.rename(path, backup)
            break
        except FileExistsError:
            continue
        except OSError:
            return None
    else:
        return None
    BROKEN.append((path, backup))
    return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def valid_id(v):
    """스페이스 id 모양 (키 파일 이름에 쓰임 → 경로 문자 금지)"""
    return bool(re.fullmatch(r"[0-9a-f]{16}", str(v or "")))


def open_space(root, known_id=None):
    """폴더를 스페이스로 연다. .dogani/space.json 이 없으면 만든다 → 스페이스 설정 dict (id 포함). 폴더가 없으면 OSError.
    known_id = 이 PC 설정에 적힌 그 폴더의 id: space.json 이 깨지거나 지워졌으면 새 id 대신 이것으로 되살림
    (id 가 바뀌면 키 파일·팀원 토큰·참여 주소 /api/s/<id> 가 모두 끊김)"""
    if not root or not os.path.isdir(root):
        raise OSError(f"폴더가 없습니다: {root}")
    path = os.path.join(meta_dir(root), SPACE_FILE)
    data = load_json(path) or {}
    if not valid_id(data.get("id")):
        sid = known_id if valid_id(known_id) else secrets.token_hex(8)
        data = {"created": dt.datetime.now().isoformat(timespec="seconds"), **data, "id": sid}
        write_json(path, data)
    return data


def space_name(root, names=None):
    """개인 스페이스 이름: 붙인 이름(설정 space_names) 있으면 그것, 없으면 폴더명"""
    if not root:
        return ""
    return (names or {}).get(root) or os.path.basename(os.path.normpath(root))
