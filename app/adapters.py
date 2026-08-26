"""Offline application and approved-local-document context adapters.

These adapters never open files for writing or send data outside the local process.
Document search is bounded to user-approved roots and returns short context snippets.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from defusedxml import ElementTree as SafeElementTree
from html import escape as xml_escape
from pathlib import Path
from typing import Any, Iterable

try:
    from engine import atomic_write_bytes, atomic_write_text
except ImportError:  # pragma: no cover - package import path
    from app.engine import atomic_write_bytes, atomic_write_text


EXCEL_CELL_RE = re.compile(r"(?<![A-Za-z0-9_])\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})(?![A-Za-z0-9_])")
EXCEL_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9_])\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\s*:\s*\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})(?![A-Za-z0-9_])",
    re.I,
)
PAGE_RE = re.compile(r"(?:page|페이지|쪽)\s*[:#]?\s*([1-9][0-9]{0,5})", re.I)
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".yaml", ".yml"}
DOCUMENT_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".xlsx", ".xlsm", ".hwp", ".hwpx"}
MAX_DOCUMENT_ROOTS = 5
MAX_DOCUMENT_FILES = 40
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_QUERY_CHARS = 4000
MAX_DOCUMENT_UPDATE_CHARS = 100_000
TEXT_MUTATION_SUFFIXES = TEXT_SUFFIXES
OFFICE_MUTATION_SUFFIXES = {".docx", ".xlsx"}
MAX_OFFICE_PACKAGE_BYTES = 20 * 1024 * 1024
DOCUMENT_PII_PATTERNS = (
    (re.compile(r"(?<!\d)\d{6}[- ]\d{7}(?!\d)"), "<주민번호 마스킹>"),
    (re.compile(r"(?<!\d)01[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"), "<전화번호 마스킹>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<이메일 마스킹>"),
)


def detect_application(window_title: str) -> str:
    """Classify a foreground window without inspecting its files."""
    title = (window_title or "").casefold()
    if any(token in title for token in ("excel", "엑셀", "microsoft 365")):
        return "excel"
    if any(token in title for token in ("acrobat", "pdf", "foxit", "알pdf")):
        return "pdf"
    if any(token in title for token in ("한글", "hwp", "한컴", "hanword")):
        return "hwp"
    if any(token in title for token in ("chrome", "edge", "firefox", "웨일", "whale", "browser")):
        return "browser"
    return "unknown"


def normalize_excel_cell_reference(reference: str) -> str:
    """Normalize and validate an A1-style Excel cell reference."""
    candidate = (reference or "").strip().replace("$", "").upper()
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", candidate)
    if not match:
        raise ValueError("Excel 셀 주소는 A1 형식이어야 합니다.")
    column, row = match.groups()
    if len(column) == 3 and column > "XFD":
        raise ValueError("Excel 열 범위(XFD)를 벗어난 셀 주소입니다.")
    if int(row) > 1_048_576:
        raise ValueError("Excel 행 범위(1,048,576)를 벗어난 셀 주소입니다.")
    return f"{column}{int(row)}"


def validate_pdf_page_number(page: Any) -> int:
    """Validate a one-based PDF page number."""
    try:
        value = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError("PDF 페이지는 정수여야 합니다.") from exc
    if not 1 <= value <= 999_999:
        raise ValueError("PDF 페이지는 1~999,999 범위여야 합니다.")
    return value


READ_ONLY_ADAPTER_SCHEMA_VERSION = 1


def build_read_only_adapter_validation(window_title: str, ocr_text: str) -> dict[str, Any]:
    """Extract bounded, read-only target hints for supported desktop applications."""
    application = detect_application(window_title)
    text = (ocr_text or "")[:12_000]
    result: dict[str, Any] = {
        "schema_version": READ_ONLY_ADAPTER_SCHEMA_VERSION,
        "application": application,
        "read_only": True,
        "targets": [],
        "warnings": [],
    }
    if application == "excel":
        targets: list[dict[str, Any]] = []
        for match in EXCEL_RANGE_RE.finditer(text):
            try:
                start = normalize_excel_cell_reference(f"{match.group(1)}{match.group(2)}")
                end = normalize_excel_cell_reference(f"{match.group(3)}{match.group(4)}")
            except ValueError:
                result["warnings"].append("Excel 범위 단서를 검증하지 못했습니다.")
                continue
            targets.append({"type": "range", "value": f"{start}:{end}"})
        for match in EXCEL_CELL_RE.finditer(text):
            try:
                value = normalize_excel_cell_reference(f"{match.group(1)}{match.group(2)}")
            except ValueError:
                continue
            targets.append({"type": "cell", "value": value})
        unique: dict[tuple[str, str], dict[str, Any]] = {(item["type"], item["value"]): item for item in targets}
        result["targets"] = list(unique.values())[:100]
        result["warnings"].append("Excel 대상은 주소 단서만 확인하며 셀을 수정하지 않습니다.")
    elif application == "pdf":
        pages: list[int] = []
        for match in PAGE_RE.finditer(text):
            try:
                page = validate_pdf_page_number(match.group(1))
            except ValueError:
                continue
            if page not in pages:
                pages.append(page)
        result["targets"] = [{"type": "page", "value": page} for page in pages[:50]]
        result["warnings"].append("PDF 대상은 페이지 번호 단서만 확인하며 문서를 수정하지 않습니다.")
    elif application in {"hwp", "browser"}:
        result["warnings"].append("현재 애플리케이션은 읽기 전용 화면 맥락만 제공하며 자동 수정하지 않습니다.")
    else:
        result["warnings"].append("지원되는 전용 대상 단서를 찾지 못했습니다.")
    return result


def build_adapter_context(window_title: str, ocr_text: str) -> dict[str, Any]:
    """Return conservative context hints for the local AI prompt."""
    application = detect_application(window_title)
    text = (ocr_text or "")[:12_000]
    context: dict[str, Any] = {"application": application, "hints": []}
    if application == "excel":
        ranges = [
            f"{match.group(1).upper()}{int(match.group(2))}:{match.group(3).upper()}{int(match.group(4))}"
            for match in EXCEL_RANGE_RE.finditer(text)
        ]
        cells = [
            f"{match.group(1).upper()}{int(match.group(2))}"
            for match in EXCEL_CELL_RE.finditer(text)
        ]
        context["cell_references"] = list(dict.fromkeys(cells))[:100]
        context["ranges"] = list(dict.fromkeys(ranges))[:50]
        context["hints"].append("Excel 셀 주소와 범위를 우선 확인하세요.")
    elif application == "pdf":
        pages = [int(match.group(1)) for match in PAGE_RE.finditer(text)]
        context["page_numbers"] = list(dict.fromkeys(pages))[:50]
        context["hints"].append("PDF 페이지 번호와 문서 상태를 확인하세요.")
    elif application == "hwp":
        context["hwp_document_detected"] = True
        context["hints"].append("한글 문서의 현재 편집 상태와 저장 여부를 확인하세요.")
    elif application == "browser":
        context["hints"].append("브라우저의 현재 탭과 주소 표시줄 상태를 확인하세요.")
    return context


def normalize_document_roots(roots: Iterable[str] | str | None) -> list[Path]:
    """Resolve at most five existing, non-file document roots."""
    if isinstance(roots, str):
        raw_roots = [item.strip() for item in roots.split(";")]
    else:
        raw_roots = [str(item).strip() for item in (roots or [])]
    resolved: list[Path] = []
    for raw in raw_roots:
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser()
            if candidate.is_symlink():
                continue
            path = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if path.is_dir() and path not in resolved:
            resolved.append(path)
        if len(resolved) >= MAX_DOCUMENT_ROOTS:
            break
    return resolved


def update_approved_text_document(
    roots: Iterable[str] | str | None,
    relative_path: str,
    expected_text: str,
    replacement: str,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Update exactly one text occurrence inside one approved root.

    This is an explicit, local-only mutation API. It requires caller confirmation,
    rejects traversal/symlinks/unsupported formats, creates a content-addressed
    backup, and replaces the target atomically. It never sends data externally.
    """
    if confirmed is not True:
        raise PermissionError("문서 변경은 명시적인 사용자 확인이 필요합니다.")
    approved = normalize_document_roots(roots)
    if len(approved) != 1:
        raise ValueError("문서 변경에는 승인된 로컬 루트 하나가 필요합니다.")
    root = approved[0]
    raw_relative = str(relative_path or "").strip()
    if not raw_relative or Path(raw_relative).is_absolute():
        raise ValueError("문서 경로는 승인된 루트 기준 상대 경로여야 합니다.")
    candidate = root / Path(raw_relative)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("문서 경로가 승인된 루트 밖에 있습니다.") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("변경 대상 문서가 없거나 symlink입니다.")
    if resolved.suffix.casefold() not in TEXT_MUTATION_SUFFIXES:
        raise ValueError("변경은 승인된 텍스트 문서 확장자만 지원합니다.")
    try:
        if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("변경 대상 문서가 허용 크기를 초과합니다.")
        current = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("문서를 UTF-8 텍스트로 읽을 수 없습니다.") from exc
    old = str(expected_text or "")
    new = str(replacement or "")
    if not old or len(old) > MAX_DOCUMENT_UPDATE_CHARS or len(new) > MAX_DOCUMENT_UPDATE_CHARS:
        raise ValueError(f"변경 문자열은 1~{MAX_DOCUMENT_UPDATE_CHARS:,}자여야 합니다.")
    if current.count(old) != 1:
        raise ValueError("변경 전 확인 문자열은 문서에서 정확히 한 번만 나타나야 합니다.")
    updated = current.replace(old, new, 1)
    before_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
    after_hash = hashlib.sha256(updated.encode("utf-8")).hexdigest()
    backup_name = f".{resolved.name}.autowork-{before_hash[:12]}.bak"
    backup = resolved.with_name(backup_name)
    try:
        atomic_write_text(backup, current)
        atomic_write_text(resolved, updated)
    except OSError as exc:
        raise OSError(f"문서 변경을 원자적으로 저장하지 못했습니다: {exc}") from exc
    return {
        "relative_path": str(resolved.relative_to(root)),
        "extension": resolved.suffix.casefold(),
        "backup_relative_path": str(backup.relative_to(root)),
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "changed_chars": len(new) - len(old),
        "confirmed": True,
    }


def update_approved_office_document(
    roots: Iterable[str] | str | None,
    relative_path: str,
    expected_text: str,
    replacement: str,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Safely update one text occurrence in a DOCX or XLSX OOXML package."""
    if confirmed is not True:
        raise PermissionError("Office 문서 변경은 명시적인 사용자 확인이 필요합니다.")
    approved = normalize_document_roots(roots)
    if len(approved) != 1:
        raise ValueError("Office 문서 변경에는 승인된 로컬 루트 하나가 필요합니다.")
    root = approved[0]
    raw_relative = str(relative_path or "").strip()
    if not raw_relative or Path(raw_relative).is_absolute():
        raise ValueError("Office 문서 경로는 승인된 루트 기준 상대 경로여야 합니다.")
    candidate = root / Path(raw_relative)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Office 문서 경로가 승인된 루트 밖에 있습니다.") from exc
    suffix = resolved.suffix.casefold()
    if suffix not in OFFICE_MUTATION_SUFFIXES:
        raise ValueError("Office 문서 변경은 .docx 또는 .xlsx만 지원합니다.")
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("변경 대상 Office 문서가 없거나 symlink입니다.")
    try:
        original = resolved.read_bytes()
    except OSError as exc:
        raise ValueError("Office 문서를 읽을 수 없습니다.") from exc
    if len(original) > MAX_OFFICE_PACKAGE_BYTES or not zipfile.is_zipfile(io.BytesIO(original)):
        raise ValueError("유효하지 않거나 허용 크기를 초과한 Office 문서입니다.")
    expected = str(expected_text or "")
    replacement_text = str(replacement or "")
    if not expected or len(expected) > MAX_DOCUMENT_UPDATE_CHARS or len(replacement_text) > MAX_DOCUMENT_UPDATE_CHARS:
        raise ValueError(f"변경 문자열은 1~{MAX_DOCUMENT_UPDATE_CHARS:,}자여야 합니다.")
    escaped_expected = xml_escape(expected, quote=False)
    escaped_replacement = xml_escape(replacement_text, quote=False)
    target_prefix = "word/" if suffix == ".docx" else "xl/"
    match_parts: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(original), "r") as archive:
            infos = archive.infolist()
            if sum(max(0, int(info.file_size)) for info in infos) > MAX_OFFICE_PACKAGE_BYTES:
                raise ValueError("Office 문서의 압축 해제 크기가 허용 범위를 초과합니다.")
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise ValueError("Office 패키지 내부 경로가 안전하지 않습니다.")
                if not name.endswith(".xml") or not name.startswith(target_prefix):
                    continue
                if suffix == ".docx" and not name.startswith("word/document"):
                    continue
                if suffix == ".xlsx" and not (name.startswith("xl/worksheets/") or name == "xl/sharedStrings.xml"):
                    continue
                match_parts.append((name, archive.read(info)))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("Office 문서 패키지를 읽을 수 없습니다.") from exc
    matches = [(name, data.count(escaped_expected.encode("utf-8"))) for name, data in match_parts]
    if sum(count for _, count in matches) != 1:
        raise ValueError("변경 전 확인 문자열은 대상 Office 문서에서 정확히 한 번만 나타나야 합니다.")
    changed_part = next(name for name, count in matches if count == 1)
    before_hash = hashlib.sha256(original).hexdigest()
    updated_buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original), "r") as source, zipfile.ZipFile(updated_buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.replace("\\", "/") == changed_part:
                data = data.replace(escaped_expected.encode("utf-8"), escaped_replacement.encode("utf-8"), 1)
            target.writestr(info, data)
    updated = updated_buffer.getvalue()
    try:
        with zipfile.ZipFile(io.BytesIO(updated), "r") as archive:
            if archive.testzip() is not None:
                raise ValueError("변경 후 Office 문서의 압축 무결성 검증에 실패했습니다.")
            SafeElementTree.fromstring(archive.read(changed_part))
    except (OSError, zipfile.BadZipFile, KeyError, SafeElementTree.ParseError) as exc:
        raise ValueError("변경 후 Office 문서를 다시 열 수 없습니다.") from exc
    if len(updated) > MAX_OFFICE_PACKAGE_BYTES:
        raise ValueError("변경 후 Office 문서가 허용 크기를 초과합니다.")
    after_hash = hashlib.sha256(updated).hexdigest()
    backup = resolved.with_name(f".{resolved.name}.autowork-{before_hash[:12]}.bak")
    try:
        atomic_write_bytes(backup, original)
        atomic_write_bytes(resolved, updated)
    except OSError as exc:
        raise OSError(f"Office 문서 변경을 원자적으로 저장하지 못했습니다: {exc}") from exc
    return {
        "relative_path": str(resolved.relative_to(root)),
        "extension": suffix,
        "changed_part": changed_part,
        "backup_relative_path": str(backup.relative_to(root)),
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "changed_chars": len(replacement_text) - len(expected),
        "confirmed": True,
    }


def verify_document_change(
    roots: Iterable[str] | str | None,
    relative_path: str,
    expected_sha256: str,
    *,
    expected_backup_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify a changed local document and its optional backup by SHA-256."""
    approved = normalize_document_roots(roots)
    if len(approved) != 1:
        raise ValueError("문서 검증에는 승인된 로컬 루트 하나가 필요합니다.")
    root = approved[0]
    raw_relative = str(relative_path or "").strip()
    if not raw_relative or Path(raw_relative).is_absolute():
        raise ValueError("문서 경로는 승인된 루트 기준 상대 경로여야 합니다.")
    try:
        resolved = (root / Path(raw_relative)).resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("문서 경로가 승인된 루트 밖에 있습니다.") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("검증 대상 문서가 없거나 symlink입니다.")
    try:
        size_limit = MAX_OFFICE_PACKAGE_BYTES if resolved.suffix.casefold() in OFFICE_MUTATION_SUFFIXES else MAX_DOCUMENT_BYTES
        if resolved.stat().st_size > size_limit:
            raise ValueError("검증 대상 문서가 허용 크기를 초과합니다.")
    except OSError as exc:
        raise ValueError("검증 대상 문서의 크기를 확인할 수 없습니다.") from exc
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("예상 SHA-256 값이 올바르지 않습니다.")
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("문서 SHA-256이 예상값과 일치하지 않습니다.")
    result: dict[str, Any] = {
        "relative_path": str(resolved.relative_to(root)),
        "sha256": actual,
        "verified": True,
    }
    if expected_backup_sha256 is not None:
        backup_expected = str(expected_backup_sha256).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", backup_expected):
            raise ValueError("예상 백업 SHA-256 값이 올바르지 않습니다.")
        candidates = sorted(
            (
                item
                for item in resolved.parent.iterdir()
                if item.name.startswith(f".{resolved.name}.autowork-") and item.name.endswith(".bak")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        backup = None
        backup_actual = ""
        for candidate in candidates:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if candidate_hash == backup_expected:
                backup = candidate
                backup_actual = candidate_hash
                break
        if backup is None:
            raise ValueError("문서 변경 백업을 찾을 수 없거나 SHA-256이 일치하지 않습니다.")
        result["backup_relative_path"] = str(backup.relative_to(root))
        result["backup_sha256"] = backup_actual
    return result


def restore_document_backup(
    roots: Iterable[str] | str | None,
    relative_path: str,
    expected_current_sha256: str,
    expected_backup_sha256: str,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Restore the verified backup over a document after explicit confirmation."""
    if confirmed is not True:
        raise PermissionError("문서 복구는 명시적인 사용자 확인이 필요합니다.")
    backup_expected = str(expected_backup_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", backup_expected):
        raise ValueError("예상 백업 SHA-256 값이 올바르지 않습니다.")
    approved = normalize_document_roots(roots)
    verification = verify_document_change(approved, relative_path, expected_current_sha256, expected_backup_sha256=backup_expected)
    root = approved[0]
    resolved = (root / Path(relative_path)).resolve()
    backup = root / Path(verification["backup_relative_path"])
    try:
        atomic_write_bytes(resolved, backup.read_bytes())
    except OSError as exc:
        raise OSError(f"문서 백업을 원자적으로 복구하지 못했습니다: {exc}") from exc
    restored_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if restored_sha != backup_expected:
        raise ValueError("복구 후 문서 SHA-256이 백업값과 일치하지 않습니다.")
    return {
        "relative_path": str(resolved.relative_to(root)),
        "restored_sha256": restored_sha,
        "backup_relative_path": str(backup.relative_to(root)),
        "confirmed": True,
    }


def _query_tokens(query: str) -> list[str]:
    cleaned = (query or "")[:MAX_QUERY_CHARS].casefold()
    return list(dict.fromkeys(token for token in re.findall(r"[\w가-힣]{2,}", cleaned) if token not in {"해줘", "해주세요", "현재", "문서"}))[:20]


def _snippet(text: str, tokens: list[str], limit: int = 700) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    lower = compact.casefold()
    positions = [lower.find(token) for token in tokens if lower.find(token) >= 0]
    start = max(0, min(positions) - 180) if positions else 0
    snippet = compact[start:start + limit]
    for pattern, replacement in DOCUMENT_PII_PATTERNS:
        snippet = pattern.sub(replacement, snippet)
    return snippet


def search_approved_documents(roots: Iterable[str] | str | None, query: str, max_files: int = MAX_DOCUMENT_FILES) -> list[dict[str, Any]]:
    """Search only bounded, user-approved local roots and return safe planning context."""
    tokens = _query_tokens(query)
    if not tokens:
        return []
    try:
        file_limit = max(1, min(int(max_files), MAX_DOCUMENT_FILES))
    except (TypeError, ValueError):
        file_limit = MAX_DOCUMENT_FILES
    matches: list[dict[str, Any]] = []
    for root in normalize_document_roots(roots):
        try:
            candidates = root.rglob("*")
        except OSError:
            continue
        for path in candidates:
            if len(matches) >= file_limit * 3:
                break
            try:
                if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in DOCUMENT_SUFFIXES:
                    continue
                relative = path.relative_to(root)
                stat = path.stat()
                if stat.st_size > MAX_DOCUMENT_BYTES:
                    continue
                name_text = path.name.casefold()
                searchable = name_text
                content = ""
                if path.suffix.casefold() in TEXT_SUFFIXES:
                    content = path.read_text(encoding="utf-8", errors="ignore")[:MAX_DOCUMENT_BYTES]
                    searchable += " " + content.casefold()
                score = sum(searchable.count(token) for token in tokens)
                if score <= 0:
                    continue
                matches.append({
                    "path": str(path),
                    "relative_path": str(relative),
                    "extension": path.suffix.casefold(),
                    "score": score,
                    "snippet": _snippet(content or path.name, tokens),
                })
            except (OSError, UnicodeError, ValueError):
                continue
    matches.sort(key=lambda item: (-int(item["score"]), str(item["relative_path"]).casefold()))
    return matches[:file_limit]


def build_document_context(roots: Iterable[str] | str | None, query: str) -> dict[str, Any]:
    """Build bounded local-document context without modifying or uploading files."""
    normalized = normalize_document_roots(roots)
    results = search_approved_documents(normalized, query)
    return {
        "approved_roots": [str(root) for root in normalized],
        "result_count": len(results),
        "results": results,
        "notice": "승인된 로컬 루트의 파일명·일부 텍스트만 계획 참고용으로 사용됩니다.",
    }
