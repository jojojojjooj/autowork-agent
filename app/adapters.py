"""Offline application and approved-local-document context adapters.

These adapters never open files for writing or send data outside the local process.
Document search is bounded to user-approved roots and returns short context snippets.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


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
