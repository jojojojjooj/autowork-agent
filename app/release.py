"""Static release checks for the offline Windows distribution.

This module never executes batch files, installs packages, or accesses the network.
It only checks repository files and reports missing or unsafe release prerequisites.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_FILES = (
    "README.md",
    "SECURITY_OFFLINE.md",
    "requirements.lock.txt",
    "install_windows.bat",
    "run_windows.bat",
    "build_windows.bat",
    "app/main.py",
    "app/engine.py",
)

BATCH_REQUIREMENTS = {
    "install_windows.bat": (
        (".venv", "가상환경 생성 경로 확인"),
        ("requirements.lock.txt", "고정 의존성 설치 확인"),
        ("--no-index", "오프라인 wheel 설치 경로 확인"),
        (".venv/READY.txt", "설치 완료 marker 확인"),
    ),
    "run_windows.bat": (
        (".venv\\READY.txt", "설치 완료 전 실행 차단 확인"),
        ("python app\\main.py", "GUI 실행 경로 확인"),
    ),
    "build_windows.bat": (
        (".venv\\READY.txt", "설치 완료 전 빌드 차단 확인"),
        ("PyInstaller", "PyInstaller 빌드 확인"),
        ("--onefile", "단일 실행 파일 빌드 확인"),
    ),
}


def _check(ok: bool, message: str) -> dict[str, Any]:
    return {"ok": bool(ok), "message": message}


def validate_release_layout(root: Path) -> dict[str, Any]:
    """Validate a Windows release layout without running any installer or script."""
    base = Path(root).resolve()
    checks: dict[str, dict[str, Any]] = {}
    for relative in REQUIRED_FILES:
        path = base / relative
        checks[f"file:{relative}"] = _check(path.is_file(), f"필수 파일 {'존재' if path.is_file() else '누락'}: {relative}")

    lock_path = base / "requirements.lock.txt"
    lock_lines = []
    if lock_path.is_file():
        try:
            lock_lines = [
                line.strip()
                for line in lock_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except (OSError, UnicodeDecodeError):
            lock_lines = []
    pinned = bool(lock_lines) and all("==" in line and not line.startswith(("-", "git+", "http://", "https://")) for line in lock_lines)
    checks["requirements:fully_pinned"] = _check(pinned, "requirements.lock.txt의 모든 항목이 == 버전으로 고정됨" if pinned else "requirements.lock.txt에 고정되지 않은 항목이 있음")

    for relative, requirements in BATCH_REQUIREMENTS.items():
        path = base / relative
        try:
            content = path.read_text(encoding="utf-8") if path.is_file() else ""
        except (OSError, UnicodeDecodeError):
            content = ""
        for marker, description in requirements:
            checks[f"script:{relative}:{marker}"] = _check(marker.casefold() in content.casefold(), description)

    failed = [name for name, result in checks.items() if not result["ok"]]
    return {
        "schema_version": 1,
        "root": str(base),
        "valid": not failed,
        "failed_checks": failed,
        "checks": checks,
        "notice": "정적 검사만 수행했으며 Windows 실행·설치·빌드를 직접 수행하지 않았습니다.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AutoWork Agent offline Windows release layout check")
    parser.add_argument("root", nargs="?", default=".", help="저장소 루트 경로")
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON 결과 출력")
    args = parser.parse_args(argv)
    result = validate_release_layout(Path(args.root))
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Windows release layout: " + ("OK" if result["valid"] else "FAILED"))
        for name in result["failed_checks"]:
            print(f"- {name}: {result['checks'][name]['message']}")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["validate_release_layout", "main"]
