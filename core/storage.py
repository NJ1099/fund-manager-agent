"""
상태 파일 입출력.

대시보드가 15초마다 폴링하므로, 부분적으로 쓰인 파일을 읽으면 JSON 파싱이 깨진다.
쓰기는 반드시 임시 파일 → 원자적 교체로 한다. 인코딩은 항상 UTF-8 로 명시한다
(Windows 기본값 cp949 로는 한국어가 든 파일에서 깨지거나 죽는다).
"""
import json
import os
import tempfile


def atomic_write(path, text):
    """같은 디렉터리에 임시 파일로 쓴 뒤 원자적으로 교체한다."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, obj, indent=2):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent))
