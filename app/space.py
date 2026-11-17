"""
스페이스 = dogani 가 여는 폴더 하나 (옵시디언 볼트와 같은 개념)

- 스페이스 설정은 그 폴더의 `.dogani/space.json` 에 둔다 → 폴더를 옮기거나 복사해도 따라감
  (v26.11.17 이전 이름 `vault.json` 은 열 때 `space.json` 으로 복사. 같은 id → 팀 키 파일 유지, 예전 exe 도 그대로 열림)
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
LEGACY_FILE = "vault.json"   # v26.11.17 이전 이름


def meta_dir(root):
    return os.path.join(root, META_DIR)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def open_space(root):
    """폴더를 스페이스로 연다. .dogani/space.json 이 없으면 만든다 → 스페이스 설정 dict (id 포함). 폴더가 없으면 OSError"""
    if not root or not os.path.isdir(root):
        raise OSError(f"폴더가 없습니다: {root}")
    path = os.path.join(meta_dir(root), SPACE_FILE)
    legacy = os.path.join(meta_dir(root), LEGACY_FILE)
    if not os.path.exists(path) and os.path.exists(legacy):
        # 복사(이동 아님): 예전 exe 가 같은 폴더를 열어도 같은 id → 팀 키 파일 그대로
        try:
            write_json(path, _read(legacy))
        except OSError:
            path = legacy   # 읽기 전용 등: 예전 파일 그대로 씀
    data = _read(path)
    if not re.fullmatch(r"[0-9a-f]{16}", str(data.get("id", ""))):   # 키 파일 이름에 쓰임 → 경로 문자 금지
        data = {"created": dt.datetime.now().isoformat(timespec="seconds"), **data, "id": secrets.token_hex(8)}
        write_json(path, data)
        if path != legacy and os.path.exists(legacy):
            try:
                write_json(legacy, data)   # 예전 exe 가 열어도 같은 id (안 그러면 키 파일이 갈라짐)
            except OSError:
                pass
    return data


def space_name(root):
    return os.path.basename(os.path.normpath(root)) if root else ""
