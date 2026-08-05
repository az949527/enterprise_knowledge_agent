from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import math
import re
from pathlib import PurePosixPath
from typing import Any, Iterator, Sequence

from app.documents.node import BoundingBox, DocumentNode, NodeType


PDF_PARSER_VERSION = "pdf_parser_v3"
PDF_READING_ORDER_VERSION = "layout_order_v2"
MIN_TEXT_CHARS_PER_DOCUMENT = 4
MARGIN_RATIO = 0.12
FULL_WIDTH_RATIO = 0.62
PAGE_NUMBER_PATTERN = re.compile(
    r"^(?:第\s*)?(?:\d+|[一二三四五六七八九十百千万]+)\s*(?:页)?"
    r"(?:\s*/\s*\d+)?$|^page\s+\d+(?:\s+of\s+\d+)?$",
    re.IGNORECASE,
)
FIGURE_CAPTION_PATTERN = re.compile(
    r"^(?:图|fig\.?|figure)\s*[\d一二三四五六七八九十]+(?:[-.－—]\d+)*",
    re.IGNORECASE,
)
TABLE_CAPTION_PATTERN = re.compile(
    r"^(?:表|table)\s*[\d一二三四五六七八九十]+(?:[-.－—]\d+)*",
    re.IGNORECASE,
)
DOI_PATTERN = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi\s*[:：]?\s*)?"
    r"10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    re.IGNORECASE,
)
CHAPTER_PATTERN = re.compile(
    r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分]"
)
NUMBERED_HEADING_PATTERN = re.compile(
    r"^([1-9]\d{0,2}(?:\.[0-9]{1,2}){0,3})"
    r"(?:\s*[\s、.)．]|(?=[\u4e00-\u9fffA-Za-z]))"
)
CHINESE_HEADING_PATTERN = re.compile(
    r"^[一二三四五六七八九十百千万]+[、.)．]"
)


class PdfTextLayerError(RuntimeError):
    pass


@dataclass(slots=True)
class _PdfLine:
    text: str
    bbox: BoundingBox
    font_size: float
    bold: bool
    source_order: int
    is_math: bool = False


@dataclass(slots=True)
class _PdfTable:
    bbox: BoundingBox
    rows: list[list[str]]
    headers: list[str]
    table_index: int
    header_rows: list[list[str]]
    has_header: bool
    detection_method: str = "find_tables"
    caption: str | None = None


@dataclass(slots=True)
class _PdfFigure:
    bbox: BoundingBox
    visual_bbox: BoundingBox
    caption_bbox: BoundingBox
    caption: str
    nearby_text: tuple[str, ...]
    figure_index: int
    caption_order: int
    visual_kind: str
    caption_aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class _PdfTableState:
    group_id: str
    node_id: str
    column_count: int
    width: float
    headers: tuple[str, ...]
    page_number: int
    can_continue: bool


@dataclass(frozen=True, slots=True)
class _PdfThreeLineRegion:
    bbox: BoundingBox
    rule_ys: tuple[float, ...]


@dataclass(slots=True)
class _PdfFormula:
    bbox: BoundingBox
    lines: tuple[_PdfLine, ...]
    source_order: int


@dataclass(slots=True)
class _PdfPageScan:
    lines: tuple[_PdfLine, ...]
    drawings: tuple[dict[str, Any], ...]
    visual_rects: tuple[tuple[BoundingBox, str], ...]


@dataclass(slots=True)
class _PdfScan:
    body_font_size: float
    repeated_margin_keys: set[str]
    meaningful_chars: int
    pages: tuple[_PdfPageScan, ...]


def iter_pdf_document_nodes(
    document: Any,
    *,
    document_id: str,
    source_path: str,
) -> Iterator[DocumentNode]:
    scan = _scan_document(document)
    required_chars = max(MIN_TEXT_CHARS_PER_DOCUMENT, len(document) * 4)
    if scan.meaningful_chars < required_chars:
        raise PdfTextLayerError(
            "PDF 没有可用文本层或文本过少，需要先执行 OCR。"
            " OCR is required before indexing."
        )

    sequence = 0
    section_stack: list[tuple[int, str]] = []
    previous_table: _PdfTableState | None = None
    pending_table: DocumentNode | None = None
    for page_index in range(len(document)):
        page = document.load_page(page_index)
        page_number = page_index + 1
        page_scan = scan.pages[page_index]
        lines = list(page_scan.lines)
        tables = _extract_page_tables(page, page_scan)
        figures = _extract_page_figures(
            lines,
            tables,
            page_scan.visual_rects,
        )
        page_nodes, section_stack, previous_table = _build_page_nodes(
            page,
            lines=lines,
            tables=tables,
            figures=figures,
            repeated_margin_keys=scan.repeated_margin_keys,
            body_font_size=scan.body_font_size,
            document_id=document_id,
            source_path=source_path,
            page_number=page_number,
            sequence_start=sequence,
            section_stack=section_stack,
            previous_table=previous_table,
        )
        if pending_table is not None:
            if (
                page_nodes
                and page_nodes[0].node_type is NodeType.TABLE
                and page_nodes[0].metadata.get("continuation")
            ):
                page_nodes[0] = _merge_cross_page_table_nodes(
                    pending_table,
                    page_nodes[0],
                )
            else:
                yield pending_table
            pending_table = None

        if (
            page_nodes
            and page_nodes[-1].node_type is NodeType.TABLE
            and page_nodes[-1].bbox is not None
            and page_nodes[-1].bbox.y1 >= float(page.rect.height) * 0.72
        ):
            yield from page_nodes[:-1]
            pending_table = page_nodes[-1]
        else:
            yield from page_nodes
        sequence += len(page_nodes)
    if pending_table is not None:
        yield pending_table


def _scan_document(document: Any) -> _PdfScan:
    margin_pages: Counter[str] = Counter()
    font_weights: Counter[float] = Counter()
    meaningful_chars = 0
    page_scans = []
    for page_index in range(len(document)):
        page = document.load_page(page_index)
        lines, page_font_weights = _extract_page_lines(page)
        drawings = tuple(page.get_drawings())
        visual_rects = tuple(_page_visual_rects(page, drawings))
        font_weights.update(page_font_weights)
        page_margin_keys = set()
        for line in lines:
            meaningful_chars += _meaningful_char_count(line.text)
            if _is_in_margin(line.bbox, float(page.rect.height)):
                key = _normalize_margin_text(line.text)
                if key:
                    page_margin_keys.add(key)
        margin_pages.update(page_margin_keys)
        page_scans.append(
            _PdfPageScan(
                lines=tuple(lines),
                drawings=drawings,
                visual_rects=visual_rects,
            )
        )

    repeat_threshold = max(2, math.ceil(len(document) * 0.5))
    repeated = {
        key
        for key, count in margin_pages.items()
        if count >= repeat_threshold
    }
    return _PdfScan(
        body_font_size=_weighted_median(font_weights) or 11.0,
        repeated_margin_keys=repeated,
        meaningful_chars=meaningful_chars,
        pages=tuple(page_scans),
    )


def _page_visual_rects(
    page: Any,
    drawings: Sequence[dict[str, Any]],
) -> list[tuple[BoundingBox, str]]:
    visual_rects: list[tuple[BoundingBox, str]] = []
    for image in page.get_images(full=True):
        for rect in page.get_image_rects(image):
            bbox = BoundingBox.from_value(rect)
            if bbox:
                visual_rects.append((bbox, "image"))
    for drawing in drawings:
        bbox = BoundingBox.from_value(drawing.get("rect"))
        if bbox and _bbox_area(bbox) >= 64:
            visual_rects.append((bbox, "vector"))
    return visual_rects


def _extract_page_lines(page: Any) -> tuple[list[_PdfLine], Counter[float]]:
    payload = page.get_text("dict", sort=False)
    lines = []
    font_weights: Counter[float] = Counter()
    source_order = 0
    for block in payload.get("blocks") or []:
        if int(block.get("type") or 0) != 0:
            continue
        for raw_line in block.get("lines") or []:
            spans = raw_line.get("spans") or []
            text = _normalize_text("".join(str(span.get("text") or "") for span in spans))
            if not text:
                continue
            bbox = BoundingBox.from_value(raw_line.get("bbox"))
            if bbox is None:
                continue
            font_size = max(
                (float(span.get("size") or 0.0) for span in spans),
                default=0.0,
            )
            bold = any(
                int(span.get("flags") or 0) & 16
                or "bold" in str(span.get("font") or "").casefold()
                for span in spans
            )
            is_math = _looks_like_math(text, spans)
            for span in spans:
                span_text = str(span.get("text") or "")
                weight = _meaningful_char_count(span_text)
                if weight:
                    font_weights[round(float(span.get("size") or 0.0), 1)] += weight
            lines.append(
                _PdfLine(
                    text=text,
                    bbox=bbox,
                    font_size=font_size,
                    bold=bold,
                    source_order=source_order,
                    is_math=is_math,
                )
            )
            source_order += 1
    return lines, font_weights


def _looks_like_math(text: str, spans: Sequence[dict[str, Any]]) -> bool:
    value = _normalize_text(text)
    if DOI_PATTERN.search(value):
        return False
    if re.fullmatch(r"[℃°%％]+", value):
        return False
    if _looks_like_formula_text(text):
        return True
    if any("\U0001d400" <= character <= "\U0001d7ff" for character in str(text or "")):
        return True
    return any(
        (
            "math" in str(span.get("font") or "").casefold()
            or str(span.get("font") or "").casefold() == "symbolmt"
        )
        and len(value) <= 30
        for span in spans
    )


def _extract_page_tables(
    page: Any,
    page_scan: _PdfPageScan,
) -> list[_PdfTable]:
    lines = page_scan.lines
    if not _should_probe_tables(
        lines,
        page_scan.drawings,
        float(page.rect.width),
    ):
        return []
    tables = []
    try:
        finder = page.find_tables()
    except Exception:
        finder = None

    if finder is not None:
        for table_index, table in enumerate(finder.tables):
            rows = [
                [_normalize_text(cell) for cell in row]
                for row in (table.extract() or [])
            ]
            rows = [row for row in rows if any(row)]
            if not rows:
                continue
            header = getattr(table, "header", None)
            header_names = [
                _normalize_text(value)
                for value in (getattr(header, "names", None) or [])
            ]
            header_rows, has_header = _table_header_rows(rows, header_names)
            headers = (
                _combine_header_rows(header_rows)
                if has_header
                else []
            )
            tables.append(
                _PdfTable(
                    bbox=BoundingBox.from_value(table.bbox),
                    rows=rows,
                    headers=headers,
                    table_index=table_index,
                    header_rows=header_rows,
                    has_header=has_header,
                )
            )

    for table in tables:
        table.caption = _nearby_table_caption(table, lines)
    three_line_tables = _extract_three_line_tables(
        lines,
        page_scan.drawings,
        float(page.rect.width),
        tables,
    )
    tables.extend(three_line_tables)
    tables.extend(_extract_caption_tables(page, lines, tables))
    for table in tables:
        if table.caption is None:
            table.caption = _nearby_table_caption(table, lines)
    return tables


def _nearby_table_caption(
    table: _PdfTable,
    lines: Sequence[_PdfLine],
) -> str | None:
    captions = [
        line
        for line in lines
        if _is_table_caption(line.text)
        and line.bbox.y1 <= table.bbox.y0
        and table.bbox.y0 - line.bbox.y1 <= 90
    ]
    if not captions:
        return None
    return max(captions, key=lambda line: line.bbox.y1).text


def _should_probe_tables(
    lines: Sequence[_PdfLine],
    drawings: Sequence[dict[str, Any]],
    page_width: float,
) -> bool:
    if any(TABLE_CAPTION_PATTERN.match(line.text) for line in lines):
        return True
    if _three_line_regions(drawings, page_width):
        return True
    short_lines = [
        line
        for line in lines
        if len(line.text) <= 48 and not line.is_math
    ]
    return _has_repeated_tabular_alignment(short_lines)


def _has_repeated_tabular_alignment(
    lines: Sequence[_PdfLine],
) -> bool:
    grouped: dict[int, list[_PdfLine]] = {}
    for line in lines:
        grouped.setdefault(round(line.bbox.y0 / 4), []).append(line)
    rows = [
        sorted(row, key=lambda line: line.bbox.x0)
        for row in grouped.values()
        if len(row) >= 3
    ]
    if len(rows) < 3:
        return False

    compatible_pairs = 0
    for index, first in enumerate(rows):
        first_anchors = [line.bbox.x0 for line in first]
        for second in rows[index + 1:]:
            second_anchors = [line.bbox.x0 for line in second]
            matched = sum(
                any(abs(anchor - candidate) <= 14 for candidate in second_anchors)
                for anchor in first_anchors
            )
            if matched >= 3:
                compatible_pairs += 1
    return compatible_pairs >= 2


def _three_line_regions(
    drawings: Sequence[dict[str, Any]],
    page_width: float,
) -> list[_PdfThreeLineRegion]:
    segments = _horizontal_rule_segments(drawings, page_width)
    if len(segments) < 3:
        return []

    groups: list[list[tuple[float, float, float]]] = []
    for segment in sorted(segments, key=lambda value: (value[0], value[1], value[2])):
        x0, x1, _y = segment
        matched = None
        for group in groups:
            group_x0 = _mean(value[0] for value in group)
            group_x1 = _mean(value[1] for value in group)
            overlap = max(0.0, min(x1, group_x1) - max(x0, group_x0))
            shorter = max(min(x1 - x0, group_x1 - group_x0), 1.0)
            if overlap / shorter >= 0.8:
                matched = group
                break
        if matched is None:
            groups.append([segment])
        else:
            matched.append(segment)

    regions = []
    for group in groups:
        rules = []
        for x0, x1, y in sorted(group, key=lambda value: value[2]):
            if rules and abs(y - rules[-1][2]) <= 2.5:
                old_x0, old_x1, old_y = rules[-1]
                rules[-1] = (min(old_x0, x0), max(old_x1, x1), (old_y + y) / 2)
            else:
                rules.append((x0, x1, y))
        if len(rules) < 3:
            continue
        x0 = min(value[0] for value in rules)
        x1 = max(value[1] for value in rules)
        rule_ys = tuple(value[2] for value in rules)
        regions.append(
            _PdfThreeLineRegion(
                bbox=BoundingBox(x0, rule_ys[0], x1, rule_ys[-1]),
                rule_ys=rule_ys,
            )
        )
    return regions


def _horizontal_rule_segments(
    drawings: Sequence[dict[str, Any]],
    page_width: float,
) -> list[tuple[float, float, float]]:
    minimum_width = max(100.0, page_width * 0.3)
    segments = []
    for drawing in drawings:
        for item in drawing.get("items") or ():
            if not item:
                continue
            kind = item[0]
            if kind == "l" and len(item) >= 3:
                first, second = item[1], item[2]
                x0, x1 = sorted((float(first.x), float(second.x)))
                y0, y1 = float(first.y), float(second.y)
                if abs(y0 - y1) <= 2 and x1 - x0 >= minimum_width:
                    segments.append((x0, x1, (y0 + y1) / 2))
            elif kind == "re" and len(item) >= 2:
                rect = item[1]
                x0, x1 = sorted((float(rect.x0), float(rect.x1)))
                if x1 - x0 >= minimum_width:
                    segments.append((x0, x1, float(rect.y0)))
                    segments.append((x0, x1, float(rect.y1)))
    return segments


def _extract_three_line_tables(
    lines: Sequence[_PdfLine],
    drawings: Sequence[dict[str, Any]],
    page_width: float,
    existing: Sequence[_PdfTable],
) -> list[_PdfTable]:
    results = []
    for region in _three_line_regions(drawings, page_width):
        if any(_bbox_overlap_ratio(region.bbox, table.bbox) >= 0.5 for table in existing):
            continue
        table_lines = [
            line
            for line in lines
            if region.bbox.x0 - 3 <= (line.bbox.x0 + line.bbox.x1) / 2 <= region.bbox.x1 + 3
            and region.bbox.y0 - 2 <= (line.bbox.y0 + line.bbox.y1) / 2 <= region.bbox.y1 + 2
            and not _is_table_caption(line.text)
        ]
        positioned_rows = _group_positioned_rows(table_lines)
        cell_rows = _positioned_rows_to_cells(positioned_rows)
        if len(cell_rows) < 2 or max((len(row) for row in cell_rows), default=0) < 2:
            continue
        header_rows, has_header = _table_header_rows(cell_rows, [])
        results.append(
            _PdfTable(
                bbox=region.bbox,
                rows=cell_rows,
                headers=_combine_header_rows(header_rows) if has_header else [],
                table_index=len(existing) + len(results),
                header_rows=header_rows,
                has_header=has_header,
                detection_method="three_line",
            )
        )
    return results


def _group_positioned_rows(
    lines: Sequence[_PdfLine],
) -> list[list[_PdfLine]]:
    rows: list[list[_PdfLine]] = []
    for line in sorted(lines, key=lambda value: (value.bbox.y0, value.bbox.x0)):
        center_y = (line.bbox.y0 + line.bbox.y1) / 2
        if not rows:
            rows.append([line])
            continue
        previous_center = _mean(
            (value.bbox.y0 + value.bbox.y1) / 2
            for value in rows[-1]
        )
        if abs(center_y - previous_center) <= 6:
            rows[-1].append(line)
        else:
            rows.append([line])
    return rows


def _positioned_rows_to_cells(
    rows: Sequence[Sequence[_PdfLine]],
) -> list[list[str]]:
    if not rows:
        return []
    anchor_candidates = sorted(
        line.bbox.x0
        for row in rows
        for line in row
    )
    anchors: list[list[float]] = []
    for value in anchor_candidates:
        if not anchors or abs(value - _mean(anchors[-1])) > 24:
            anchors.append([value])
        else:
            anchors[-1].append(value)
    column_anchors = [_mean(cluster) for cluster in anchors]
    if len(column_anchors) < 2:
        return []

    cells = []
    for row in rows:
        values = [""] * len(column_anchors)
        for line in sorted(row, key=lambda value: value.bbox.x0):
            index = min(
                range(len(column_anchors)),
                key=lambda item: abs(line.bbox.x0 - column_anchors[item]),
            )
            values[index] = " ".join(
                value for value in (values[index], line.text) if value
            )
        cells.append(values)
    return cells


def _table_header_rows(
    rows: list[list[str]],
    header_names: list[str],
) -> tuple[list[list[str]], bool]:
    if not rows:
        return [], False
    first = [_normalize_text(value) for value in rows[0]]
    if not _looks_like_header_row(first):
        stable_names = _stable_header_names(header_names, first)
        return ([stable_names] if stable_names else []), bool(stable_names)

    candidates = [first]
    if len(rows) > 1 and _is_multilevel_header(first, rows[1]):
        candidates.append([_normalize_text(value) for value in rows[1]])
    return candidates, True


def _stable_header_names(
    header_names: Sequence[str],
    first_row: Sequence[str],
) -> list[str]:
    names = [_normalize_text(value) for value in header_names]
    if not names or len(names) != len(first_row):
        return []
    first_values = [_normalize_text(value) for value in first_row]
    if all(
        not first or name == first or name.startswith(first + ".")
        for name, first in zip(names, first_values)
    ):
        return names
    return []


def _is_multilevel_header(
    first_row: Sequence[str],
    second_row: Sequence[str],
) -> bool:
    first = [_normalize_text(value) for value in first_row]
    second = [_normalize_text(value) for value in second_row]
    if not _looks_like_header_row(second):
        return False
    has_parent_spans = any(not value for value in first) or len(set(first)) < len(first)
    return has_parent_spans and (
        _contains_metric_headers(second)
        or sum(bool(value) for value in second) >= max(2, len(second) // 2)
    )


def _looks_like_header_row(row: Sequence[str]) -> bool:
    values = [str(value or "").strip() for value in row]
    non_empty = [value for value in values if value]
    if not non_empty:
        return False
    numeric = sum(bool(re.fullmatch(r"[-+]?[\d.]+%?", value)) for value in non_empty)
    return numeric / len(non_empty) < 0.5


def _contains_metric_headers(row: Sequence[str]) -> bool:
    joined = " ".join(str(value or "") for value in row).casefold()
    return any(
        marker in joined
        for marker in ("rc", "rmsec", "rp", "rmsep", "均值", "平均", "标准差")
    )


def _combine_header_rows(rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    normalized: list[list[str]] = [
        list(row) + [""] * (width - len(row))
        for row in rows
    ]
    if len(normalized) == 1:
        return normalized[0]
    for row_index in range(len(normalized) - 1):
        parent = ""
        for column in range(width):
            value = normalized[row_index][column].strip()
            if value:
                parent = value
            elif parent:
                normalized[row_index][column] = parent
    combined = []
    for column in range(width):
        parts = [
            normalized[row][column].strip()
            for row in range(len(normalized))
            if normalized[row][column].strip()
        ]
        combined.append(".".join(dict.fromkeys(parts)))
    return combined


def _extract_caption_tables(
    page: Any,
    lines: Sequence[_PdfLine],
    existing: Sequence[_PdfTable],
) -> list[_PdfTable]:
    results = []
    for caption in (line for line in lines if TABLE_CAPTION_PATTERN.match(line.text)):
        if any(
            table.bbox.y0 >= caption.bbox.y0
            and table.bbox.y0 - caption.bbox.y1 < 80
            for table in existing
        ):
            continue
        candidate_lines = [
            line
            for line in lines
            if line.bbox.y0 > caption.bbox.y1 + 8
            and line.bbox.y0 < float(page.rect.height) * 0.92
        ]
        rows = _caption_table_rows(candidate_lines)
        if len(rows) < 2:
            continue
        table_lines = [line for row in rows for line in row]
        bbox = _union_bbox([line.bbox for line in table_lines])
        cell_rows = _caption_rows_to_cells(rows)
        if len(cell_rows) < 2:
            continue
        results.append(
            _PdfTable(
                bbox=bbox,
                rows=cell_rows,
                headers=_combine_header_rows([cell_rows[0]]),
                table_index=len(existing) + len(results),
                header_rows=[cell_rows[0]],
                has_header=True,
                caption=caption.text,
            )
        )
    return results


def _caption_table_rows(
    lines: Sequence[_PdfLine],
) -> list[list[_PdfLine]]:
    rows: list[list[_PdfLine]] = []
    last_y = None
    for line in sorted(lines, key=lambda value: (value.bbox.y0, value.bbox.x0)):
        if _is_section_like_text(line.text) and rows:
            break
        if len(line.text) > 60 and rows:
            break
        if last_y is None or line.bbox.y0 - last_y <= 8:
            if not rows:
                rows.append([])
            rows[-1].append(line)
        else:
            if len(rows) >= 2 and line.bbox.y0 - last_y > 28:
                break
            rows.append([line])
        last_y = line.bbox.y0
    return rows


def _caption_rows_to_cells(
    rows: Sequence[Sequence[_PdfLine]],
) -> list[list[str]]:
    if not rows:
        return []
    anchors = sorted(
        {
            round(line.bbox.x0, 1)
            for row in rows[:2]
            for line in row
        }
    )
    if len(anchors) < 3:
        return []
    cells = []
    for row in rows:
        values = [""] * len(anchors)
        for line in sorted(row, key=lambda value: value.bbox.x0):
            index = min(
                range(len(anchors)),
                key=lambda item: abs(line.bbox.x0 - anchors[item]),
            )
            values[index] = " ".join(
                value for value in (values[index], line.text) if value
            )
        cells.append(values)
    return cells


def _extract_page_figures(
    lines: Sequence[_PdfLine],
    tables: Sequence[_PdfTable],
    page_visual_rects: Sequence[tuple[BoundingBox, str]],
) -> list[_PdfFigure]:
    captions = [
        line
        for line in lines
        if _is_figure_caption_candidate(line.text)
    ]
    if not captions:
        return []
    visual_rects = [
        (bbox, kind)
        for bbox, kind in page_visual_rects
        if not any(_bbox_overlap_ratio(bbox, table.bbox) > 0.5 for table in tables)
    ]

    figures = []
    previous_caption_y = 0.0
    for figure_index, caption in enumerate(captions):
        if figures and _is_duplicate_figure_caption(figures[-1], caption):
            previous = figures[-1]
            figures[-1] = replace(
                previous,
                caption_aliases=(
                    *previous.caption_aliases,
                    caption.text,
                ),
            )
            previous_caption_y = caption.bbox.y1
            continue
        lower_y = max(previous_caption_y, caption.bbox.y0 - 280)
        candidates = [
            (bbox, kind)
            for bbox, kind in visual_rects
            if lower_y <= bbox.y1 <= caption.bbox.y0 + 2
        ]
        if candidates:
            visual_bbox = _union_bbox([item[0] for item in candidates])
            kinds = {item[1] for item in candidates}
            visual_kind = "mixed" if len(kinds) > 1 else next(iter(kinds))
        else:
            visual_bbox = caption.bbox
            visual_kind = "caption_only"
        nearby_text = tuple(
            line.text
            for line in _nearby_figure_lines(
                caption,
                visual_bbox,
                lines,
                tables,
            )
        )
        figures.append(
            _PdfFigure(
                bbox=_union_bbox([visual_bbox, caption.bbox]),
                visual_bbox=visual_bbox,
                caption_bbox=caption.bbox,
                caption=caption.text,
                nearby_text=nearby_text,
                figure_index=figure_index,
                caption_order=caption.source_order,
                visual_kind=visual_kind,
            )
        )
        previous_caption_y = caption.bbox.y1
    return figures


def _is_figure_caption_candidate(text: str) -> bool:
    value = _normalize_text(text)
    if not FIGURE_CAPTION_PATTERN.match(value):
        return False
    if len(value) > 100:
        return False
    return not re.match(
        r"^(?:图|fig\.?|figure)\s*[\d一二三四五六七八九十]+"
        r"\s*为",
        value,
        re.IGNORECASE,
    )


def _figure_number(text: str) -> str | None:
    match = re.match(
        r"^(?:图|fig\.?|figure)\s*([\d一二三四五六七八九十]+)",
        _normalize_text(text),
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _is_duplicate_figure_caption(
    previous: _PdfFigure,
    caption: _PdfLine,
) -> bool:
    previous_number = _figure_number(previous.caption)
    current_number = _figure_number(caption.text)
    if not previous_number or previous_number != current_number:
        return False
    return -4 <= caption.bbox.y0 - previous.caption_bbox.y1 <= 70


def _nearby_figure_lines(
    caption: _PdfLine,
    visual_bbox: BoundingBox,
    lines: Sequence[_PdfLine],
    tables: Sequence[_PdfTable],
) -> list[_PdfLine]:
    candidates = []
    figure_bbox = _union_bbox([caption.bbox, visual_bbox])
    for line in lines:
        if line.source_order == caption.source_order:
            continue
        if line.is_math or _is_figure_caption(line.text) or _is_table_caption(line.text):
            continue
        if any(_bbox_overlap_ratio(line.bbox, table.bbox) >= 0.5 for table in tables):
            continue
        vertical_distance = min(
            abs(line.bbox.y1 - figure_bbox.y0),
            abs(line.bbox.y0 - figure_bbox.y1),
        )
        if vertical_distance <= 96:
            candidates.append((vertical_distance, line.source_order, line))
    return [item[2] for item in sorted(candidates)[:3]]


def _is_figure_caption(text: str) -> bool:
    return bool(FIGURE_CAPTION_PATTERN.match(str(text or "").strip()))


def _is_table_caption(text: str) -> bool:
    return bool(TABLE_CAPTION_PATTERN.match(str(text or "").strip()))


def _is_section_like_text(text: str) -> bool:
    value = str(text or "").strip()
    return bool(
        NUMBERED_HEADING_PATTERN.match(value)
        or CHINESE_HEADING_PATTERN.match(value)
        or CHAPTER_PATTERN.match(value)
    )


def _build_page_nodes(
    page: Any,
    *,
    lines: list[_PdfLine],
    tables: list[_PdfTable],
    figures: list[_PdfFigure],
    repeated_margin_keys: set[str],
    body_font_size: float,
    document_id: str,
    source_path: str,
    page_number: int,
    sequence_start: int,
    section_stack: list[tuple[int, str]],
    previous_table: _PdfTableState | None,
) -> tuple[
    list[DocumentNode],
    list[tuple[int, str]],
    _PdfTableState | None,
]:
    page_height = float(page.rect.height)
    removed_margin_lines = 0
    filtered_lines = []
    figure_orders = {figure.caption_order for figure in figures}
    for line in lines:
        if _is_margin_noise(line, page_height, repeated_margin_keys):
            removed_margin_lines += 1
            continue
        if any(_bbox_overlap_ratio(line.bbox, table.bbox) >= 0.5 for table in tables):
            continue
        if line.source_order in figure_orders:
            continue
        filtered_lines.append(line)

    formulas, filtered_lines = _extract_formula_regions(filtered_lines)
    filtered_lines = _merge_heading_lines(filtered_lines, body_font_size)
    items: list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula] = [
        *filtered_lines,
        *formulas,
        *tables,
        *figures,
    ]
    ordered_items = _sort_layout_items(items, float(page.rect.width))
    nodes = []
    text_buffer: list[_PdfLine] = []
    buffer_heading: str | None = None
    buffer_heading_level: int | None = None

    def flush_text() -> None:
        nonlocal text_buffer, buffer_heading, buffer_heading_level
        if not text_buffer:
            return
        bbox = _union_bbox([line.bbox for line in text_buffer])
        content = "\n".join(line.text for line in text_buffer)
        metadata = {
            "filename": PurePosixPath(source_path).name,
            "file_type": ".pdf",
            "line_count": len(text_buffer),
            "body_font_size": body_font_size,
            "reading_order_version": PDF_READING_ORDER_VERSION,
            "removed_margin_lines": removed_margin_lines,
        }
        if buffer_heading:
            metadata["heading"] = buffer_heading
            metadata["heading_level"] = buffer_heading_level
        nodes.append(
            DocumentNode(
                document_id=document_id,
                content=content,
                parser_version=PDF_PARSER_VERSION,
                node_type=NodeType.TEXT,
                page_or_sheet=page_number,
                section_path=tuple(text for _, text in section_stack),
                sequence=sequence_start + len(nodes),
                bbox=bbox,
                source_anchor={
                    "source_path": source_path,
                    "page": page_number,
                    "bbox": bbox.to_list(),
                },
                metadata=metadata,
            )
        )
        text_buffer = []
        buffer_heading = None
        buffer_heading_level = None

    for item in ordered_items:
        if isinstance(item, _PdfTable):
            flush_text()
            continuation = _is_table_continuation(
                item,
                previous_table,
                page_number=page_number,
                page_height=page_height,
            )
            group_id = (
                previous_table.group_id
                if continuation and previous_table
                else f"{document_id}:table:{sequence_start + len(nodes)}"
            )
            parent_id = previous_table.node_id if continuation and previous_table else None
            inherited_headers = (
                list(previous_table.headers)
                if continuation and previous_table
                else None
            )
            data_only_continuation = bool(
                continuation
                and previous_table
                and _table_is_data_only_continuation(
                    item,
                    previous_table.headers,
                )
            )
            table_node = _table_node(
                item,
                document_id=document_id,
                source_path=source_path,
                page_number=page_number,
                sequence=sequence_start + len(nodes),
                section_path=tuple(text for _, text in section_stack),
                group_id=group_id,
                parent_id=parent_id,
                continuation=continuation,
                headers_override=inherited_headers,
                header_rows_override=[] if data_only_continuation else None,
            )
            nodes.append(table_node)
            state_headers = (
                tuple(previous_table.headers)
                if continuation and previous_table
                else tuple(item.headers)
            )
            previous_table = _PdfTableState(
                group_id=group_id,
                node_id=table_node.node_id if not continuation else (
                    previous_table.node_id if previous_table else table_node.node_id
                ),
                column_count=max((len(row) for row in item.rows), default=0),
                width=item.bbox.x1 - item.bbox.x0,
                headers=state_headers,
                page_number=page_number,
                can_continue=item.bbox.y1 >= page_height * 0.72,
            )
            continue

        if isinstance(item, _PdfFigure):
            flush_text()
            nodes.append(
                _figure_node(
                    item,
                    document_id=document_id,
                    source_path=source_path,
                    page_number=page_number,
                    sequence=sequence_start + len(nodes),
                    section_path=tuple(text for _, text in section_stack),
                )
            )
            continue

        if isinstance(item, _PdfFormula):
            flush_text()
            nodes.append(
                _formula_node(
                    item,
                    document_id=document_id,
                    source_path=source_path,
                    page_number=page_number,
                    sequence=sequence_start + len(nodes),
                    section_path=tuple(text for _, text in section_stack),
                )
            )
            continue

        heading_level = (
            None
            if _is_heading_role_noise(
                item,
                page_number=page_number,
                page_height=page_height,
                body_font_size=body_font_size,
            )
            else _heading_level(item, body_font_size)
        )
        if heading_level is not None:
            flush_text()
            section_stack = _updated_section_stack(
                section_stack,
                heading_level,
                item.text,
            )
            buffer_heading = item.text
            buffer_heading_level = heading_level
        text_buffer.append(item)

    flush_text()
    return nodes, section_stack, previous_table


def _is_table_continuation(
    table: _PdfTable,
    previous: _PdfTableState | None,
    *,
    page_number: int,
    page_height: float,
) -> bool:
    if not previous or not previous.can_continue:
        return False
    if previous.page_number != page_number - 1:
        return False
    if table.caption:
        return False
    if table.bbox.y0 > page_height * 0.22:
        return False
    if max((len(row) for row in table.rows), default=0) != previous.column_count:
        return False
    width = table.bbox.x1 - table.bbox.x0
    if abs(width - previous.width) >= max(previous.width * 0.15, 30.0):
        return False
    if _table_is_data_only_continuation(table, previous.headers):
        return True
    return _headers_are_compatible(table, previous.headers)


def _headers_are_compatible(
    table: _PdfTable,
    previous_headers: Sequence[str],
) -> bool:
    current = {
        _header_key(value)
        for value in table.headers
        if _header_key(value)
    }
    previous = {
        _header_key(value)
        for value in previous_headers
        if _header_key(value)
    }
    return bool(current and previous and len(current & previous) >= 2)


def _table_is_data_only_continuation(
    table: _PdfTable,
    previous_headers: Sequence[str],
) -> bool:
    if not table.rows:
        return False
    first = [str(value or "").strip() for value in table.rows[0]]
    values = [value for value in first if value]
    if not values:
        return True
    numeric = sum(
        bool(re.fullmatch(r"[-+]?[\d.]+%?", value))
        for value in values
    )
    if numeric / len(values) >= 0.45:
        return True
    return not _headers_are_compatible(table, previous_headers)


def _header_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _table_node(
    table: _PdfTable,
    *,
    document_id: str,
    source_path: str,
    page_number: int,
    sequence: int,
    section_path: tuple[str, ...],
    group_id: str,
    parent_id: str | None,
    continuation: bool,
    headers_override: list[str] | None = None,
    header_rows_override: list[list[str]] | None = None,
) -> DocumentNode:
    rows = _merge_wrapped_table_rows(table.rows)
    column_count = max((len(row) for row in rows), default=0)
    normalized_rows = [
        row + [""] * (column_count - len(row))
        for row in rows
    ]
    source_headers = (
        headers_override
        if headers_override is not None
        else table.headers
    )
    headers = source_headers + [""] * (column_count - len(source_headers))
    header_rows = (
        header_rows_override
        if header_rows_override is not None
        else table.header_rows
    )
    content = "\n".join("\t".join(row) for row in normalized_rows)
    # 展示文本延迟生成：索引只保存结构（metadata.cells/headers），
    # 需要展示时由 render_table_display 按需渲染。
    return DocumentNode(
        document_id=document_id,
        content=content,
        display_content=None,
        parser_version=PDF_PARSER_VERSION,
        node_type=NodeType.TABLE,
        page_or_sheet=page_number,
        section_path=section_path,
        sequence=sequence,
        bbox=table.bbox,
        row_start=1,
        row_end=len(normalized_rows),
        column_start=1,
        column_end=column_count,
        parent_id=parent_id,
        source_anchor={
            "source_path": source_path,
            "page": page_number,
            "bbox": table.bbox.to_list(),
            "table_index": table.table_index,
            "row_start": 1,
            "row_end": len(normalized_rows),
            "column_start": 1,
            "column_end": column_count,
            "table_group_id": group_id,
            "continuation": continuation,
        },
        metadata={
            "filename": PurePosixPath(source_path).name,
            "file_type": ".pdf",
            "table_index": table.table_index,
            "headers": headers,
            "header_rows": header_rows,
            "cells": normalized_rows,
            "row_count": len(normalized_rows),
            "column_count": column_count,
            "reading_order_version": PDF_READING_ORDER_VERSION,
            "table_group_id": group_id,
            "continuation": continuation,
            "detection_method": table.detection_method,
        },
    )


def _merge_wrapped_table_rows(
    rows: Sequence[Sequence[str]],
) -> list[list[str]]:
    result: list[list[str]] = []
    index = 0
    while index < len(rows):
        current = [str(value or "").strip() for value in rows[index]]
        if (
            index + 1 < len(rows)
            and _is_label_only_row(current)
            and _looks_like_wrapped_label(current[0], rows[index + 1])
        ):
            following = [str(value or "").strip() for value in rows[index + 1]]
            following[0] = current[0] + following[0]
            result.append(following)
            index += 2
            continue
        result.append(current)
        index += 1
    return result


def _is_label_only_row(row: Sequence[str]) -> bool:
    values = [str(value or "").strip() for value in row]
    return bool(values and values[0]) and not any(values[1:])


def _looks_like_wrapped_label(
    first_label: str,
    next_row: Sequence[str],
) -> bool:
    next_values = [str(value or "").strip() for value in next_row]
    if not next_values or not next_values[0]:
        return False
    if not any(next_values[1:]):
        return False
    return (
        len(first_label) >= 4
        and (
            next_values[0].startswith(("度", "率", "量", "值", "数"))
            or re.fullmatch(r"[\u4e00-\u9fff]{1,3}[（(].*", next_values[0])
        )
    )


def _merge_cross_page_table_nodes(
    first: DocumentNode,
    continuation: DocumentNode,
) -> DocumentNode:
    first_rows = [
        list(row)
        for row in first.metadata.get("cells") or []
    ]
    continuation_rows = [
        list(row)
        for row in continuation.metadata.get("cells") or []
    ]
    headers = list(first.metadata.get("headers") or [])
    continuation_headers = list(continuation.metadata.get("headers") or [])
    if headers and headers == continuation_headers:
        repeated_header_count = len(
            continuation.metadata.get("header_rows") or []
        )
        continuation_rows = continuation_rows[repeated_header_count:]

    rows = first_rows + continuation_rows
    page_bboxes = list(first.metadata.get("page_bboxes") or [])
    if not page_bboxes:
        page_bboxes.append(
            {
                "page": first.source_anchor.get("page"),
                "bbox": first.source_anchor.get("bbox"),
            }
        )
    page_bboxes.append(
        {
            "page": continuation.source_anchor.get("page"),
            "bbox": continuation.source_anchor.get("bbox"),
        }
    )
    pages = [
        int(item["page"])
        for item in page_bboxes
        if item.get("page") is not None
    ]
    metadata = dict(first.metadata)
    metadata.update(
        {
            "cells": rows,
            "row_count": len(rows),
            "cross_page": True,
            "page_start": min(pages),
            "page_end": max(pages),
            "page_bboxes": page_bboxes,
            "continuation_pages": sorted(set(pages[1:])),
        }
    )
    source_anchor = dict(first.source_anchor)
    source_anchor.update(
        {
            "page_start": min(pages),
            "page_end": max(pages),
            "page_bboxes": page_bboxes,
            "row_end": len(rows),
        }
    )
    content = "\n".join("\t".join(row) for row in rows)
    return DocumentNode(
        document_id=first.document_id,
        content=content,
        display_content=None,
        parser_version=first.parser_version,
        node_type=NodeType.TABLE,
        node_id=first.node_id,
        page_or_sheet=first.page_or_sheet,
        section_path=first.section_path,
        sequence=first.sequence,
        bbox=first.bbox,
        row_start=1,
        row_end=len(rows),
        column_start=first.column_start,
        column_end=first.column_end,
        parent_id=first.parent_id,
        source_anchor=source_anchor,
        metadata=metadata,
    )


def _figure_node(
    figure: _PdfFigure,
    *,
    document_id: str,
    source_path: str,
    page_number: int,
    sequence: int,
    section_path: tuple[str, ...],
) -> DocumentNode:
    captions = [figure.caption, *figure.caption_aliases]
    content_parts = captions.copy()
    if figure.nearby_text:
        content_parts.append("附近正文：" + " ".join(figure.nearby_text))
    return DocumentNode(
        document_id=document_id,
        content="\n".join(content_parts),
        parser_version=PDF_PARSER_VERSION,
        node_type=NodeType.FIGURE,
        page_or_sheet=page_number,
        section_path=section_path,
        sequence=sequence,
        bbox=figure.bbox,
        source_anchor={
            "source_path": source_path,
            "page": page_number,
            "bbox": figure.bbox.to_list(),
            "visual_bbox": figure.visual_bbox.to_list(),
            "caption_bbox": figure.caption_bbox.to_list(),
            "caption_bboxes": [figure.caption_bbox.to_list()],
            "figure_index": figure.figure_index,
        },
        metadata={
            "filename": PurePosixPath(source_path).name,
            "file_type": ".pdf",
            "caption": figure.caption,
            "captions": captions,
            "nearby_text": list(figure.nearby_text),
            "figure_index": figure.figure_index,
            "visual_kind": figure.visual_kind,
            "reading_order_version": PDF_READING_ORDER_VERSION,
        },
    )


def _formula_node(
    formula: _PdfFormula,
    *,
    document_id: str,
    source_path: str,
    page_number: int,
    sequence: int,
    section_path: tuple[str, ...],
) -> DocumentNode:
    content = "\n".join(line.text for line in formula.lines)
    return DocumentNode(
        document_id=document_id,
        content=content,
        parser_version=PDF_PARSER_VERSION,
        node_type=NodeType.TEXT,
        page_or_sheet=page_number,
        section_path=section_path,
        sequence=sequence,
        bbox=formula.bbox,
        source_anchor={
            "source_path": source_path,
            "page": page_number,
            "bbox": formula.bbox.to_list(),
            "content_kind": "formula",
        },
        metadata={
            "filename": PurePosixPath(source_path).name,
            "file_type": ".pdf",
            "content_kind": "formula",
            "line_count": len(formula.lines),
            "reading_order_version": PDF_READING_ORDER_VERSION,
        },
    )


def _sort_layout_items(
    items: Sequence[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula],
    page_width: float,
) -> list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula]:
    if len(items) < 2:
        return list(items)

    midpoint = page_width / 2
    full_width: list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula] = []
    narrow: list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula] = []
    for item in items:
        bbox = item.bbox
        width = bbox.x1 - bbox.x0
        if width >= page_width * FULL_WIDTH_RATIO or (
            bbox.x0 < midpoint < bbox.x1
        ):
            full_width.append(item)
        else:
            narrow.append(item)

    full_width.sort(key=lambda item: (item.bbox.y0, item.bbox.x0))
    result: list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula] = []
    remaining = list(narrow)
    for separator in full_width:
        before = [
            item
            for item in remaining
            if (item.bbox.y0 + item.bbox.y1) / 2 < separator.bbox.y0
        ]
        result.extend(_sort_column_band(before, page_width))
        before_ids = {id(item) for item in before}
        remaining = [item for item in remaining if id(item) not in before_ids]
        result.append(separator)
    result.extend(_sort_column_band(remaining, page_width))
    return result


def _sort_column_band(
    items: Sequence[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula],
    page_width: float,
) -> list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula]:
    if len(items) < 2:
        return list(items)

    if not _has_confident_columns(items, page_width):
        return _sort_baseline_items(items)

    tolerance = max(30.0, page_width * 0.12)
    columns: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: (value.bbox.x0, value.bbox.y0)):
        nearest = min(
            columns,
            key=lambda column: abs(item.bbox.x0 - column["anchor"]),
            default=None,
        )
        if nearest is None or abs(item.bbox.x0 - nearest["anchor"]) > tolerance:
            columns.append({"anchor": item.bbox.x0, "items": [item]})
        else:
            nearest["items"].append(item)
            nearest["anchor"] = _mean(
                value.bbox.x0 for value in nearest["items"]
            )

    if len(columns) == 1 or len(columns) > 4:
        return _sort_baseline_items(items)

    ordered = []
    for column in sorted(columns, key=lambda value: value["anchor"]):
        ordered.extend(
            _sort_baseline_items(column["items"])
        )
    return ordered


def _sort_baseline_items(
    items: Sequence[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula],
) -> list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula]:
    if all(isinstance(item, _PdfLine) for item in items):
        return sorted(items, key=lambda item: item.source_order)
    ordered = sorted(items, key=lambda item: (item.bbox.y0, item.bbox.x0))
    bands: list[list[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula]] = []
    for item in ordered:
        if not bands:
            bands.append([item])
            continue
        previous = bands[-1][0]
        if abs(item.bbox.y0 - previous.bbox.y0) <= 2.5:
            bands[-1].append(item)
        else:
            bands.append([item])

    result = []
    for band in bands:
        result.extend(
            sorted(
                band,
                key=lambda item: (
                    item.bbox.x0,
                    item.source_order
                    if isinstance(item, _PdfLine)
                    else 0,
                ),
            )
        )
    return result


def _has_confident_columns(
    items: Sequence[_PdfLine | _PdfTable | _PdfFigure | _PdfFormula],
    page_width: float,
) -> bool:
    candidates = [
        item
        for item in items
        if isinstance(item, _PdfLine) and not item.is_math
    ]
    if len(candidates) < 4:
        return False
    tolerance = max(24.0, page_width * 0.06)
    clusters: list[list[_PdfLine]] = []
    for item in sorted(candidates, key=lambda value: value.bbox.x0):
        if (
            not clusters
            or abs(item.bbox.x0 - _mean(line.bbox.x0 for line in clusters[-1]))
            > tolerance
        ):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    if len(clusters) < 2 or len(clusters) > 4:
        return False
    if not all(len(cluster) >= 2 for cluster in clusters):
        return False

    boxes = [
        _union_bbox([line.bbox for line in cluster])
        for cluster in clusters
    ]
    if any(box.y1 - box.y0 < 24 for box in boxes):
        return False
    minimum_gutter = max(18.0, page_width * 0.035)
    for left, right in zip(boxes, boxes[1:]):
        if right.x0 - left.x1 < minimum_gutter:
            return False
        overlap = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
        shorter_span = max(min(left.y1 - left.y0, right.y1 - right.y0), 1.0)
        if overlap / shorter_span < 0.45:
            return False
    return True


def _merge_heading_lines(
    lines: Sequence[_PdfLine],
    body_font_size: float,
) -> list[_PdfLine]:
    ordered = sorted(lines, key=lambda line: line.source_order)
    result: list[_PdfLine] = []
    index = 0
    while index < len(ordered):
        current = ordered[index]
        if (
            index + 1 < len(ordered)
            and _is_title_line(current, body_font_size)
            and _is_title_line(ordered[index + 1], body_font_size)
            and _can_merge_title_lines(current, ordered[index + 1])
        ):
            following = ordered[index + 1]
            result.append(
                _PdfLine(
                    text=f"{current.text} {following.text}".strip(),
                    bbox=_union_bbox([current.bbox, following.bbox]),
                    font_size=max(current.font_size, following.font_size),
                    bold=current.bold or following.bold,
                    source_order=current.source_order,
                    is_math=False,
                )
            )
            index += 2
            continue
        result.append(current)
        index += 1
    return result


def _extract_formula_regions(
    lines: Sequence[_PdfLine],
) -> tuple[list[_PdfFormula], list[_PdfLine]]:
    math_lines = [line for line in lines if line.is_math]
    remaining = [line for line in lines if not line.is_math]
    regions: list[list[_PdfLine]] = []
    for line in sorted(math_lines, key=lambda value: (value.bbox.y0, value.bbox.x0)):
        matching = [
            region
            for region in regions
            if _formula_line_touches_region(line, region)
        ]
        if not matching:
            regions.append([line])
            continue
        target = matching[0]
        target.append(line)
        for duplicate in matching[1:]:
            target.extend(duplicate)
            regions.remove(duplicate)
    formulas = []
    for region in regions:
        if _is_substantive_formula_region(region):
            formulas.append(_formula_from_lines(region))
        else:
            remaining.extend(region)
    remaining.sort(key=lambda value: value.source_order)
    return formulas, remaining


def _is_substantive_formula_region(
    lines: Sequence[_PdfLine],
) -> bool:
    content = "".join(line.text for line in lines)
    compact = re.sub(r"\s+", "", content)
    if len(compact) < 8:
        return False
    if any(marker in compact for marker in ("=", "＝", "∑", "√", "∫", "≤", "≥")):
        return True
    if any(
        re.search(
            rf"\b{name}\s*\(",
            content,
            re.IGNORECASE,
        )
        for name in ("sqrt", "sum", "log", "ln", "exp", "mean", "var")
    ):
        return True
    operators = sum(character in "+-−*/×÷^_" for character in compact)
    digits = sum(character.isdigit() for character in compact)
    return len(lines) >= 2 and operators >= 3 and digits >= 1


def _formula_line_touches_region(
    line: _PdfLine,
    region: Sequence[_PdfLine],
) -> bool:
    region_bbox = _union_bbox([value.bbox for value in region])
    vertical_gap = max(
        region_bbox.y0 - line.bbox.y1,
        line.bbox.y0 - region_bbox.y1,
        0.0,
    )
    center_gap = abs(
        (line.bbox.x0 + line.bbox.x1)
        - (region_bbox.x0 + region_bbox.x1)
    ) / 2
    horizontal_overlap = max(
        0.0,
        min(line.bbox.x1, region_bbox.x1)
        - max(line.bbox.x0, region_bbox.x0),
    )
    return (
        vertical_gap <= max(28.0, line.font_size * 2.5)
        and (horizontal_overlap > 0 or center_gap <= 140)
    )


def _formula_from_lines(lines: Sequence[_PdfLine]) -> _PdfFormula:
    ordered = tuple(
        sorted(lines, key=lambda line: (line.bbox.y0, line.bbox.x0, line.source_order))
    )
    return _PdfFormula(
        bbox=_union_bbox([line.bbox for line in ordered]),
        lines=ordered,
        source_order=min(line.source_order for line in ordered),
    )


def _is_title_line(line: _PdfLine, body_font_size: float) -> bool:
    return (
        not line.is_math
        and line.font_size >= body_font_size * 1.35
        and not _is_non_heading(line)
    )


def _can_merge_title_lines(first: _PdfLine, second: _PdfLine) -> bool:
    first_center = (first.bbox.x0 + first.bbox.x1) / 2
    second_center = (second.bbox.x0 + second.bbox.x1) / 2
    return (
        abs(first_center - second_center) <= 35
        and 0 <= second.bbox.y0 - first.bbox.y1 <= 24
    )


def _heading_level(line: _PdfLine, body_font_size: float) -> int | None:
    text = line.text.strip()
    if not text or len(text) > 48 or _is_non_heading(line):
        return None
    if CHAPTER_PATTERN.match(text):
        return 1
    numbered = NUMBERED_HEADING_PATTERN.match(text)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 4)
    if CHINESE_HEADING_PATTERN.match(text):
        return 2

    ratio = line.font_size / max(body_font_size, 1.0)
    if ratio >= 1.45:
        return 1
    if ratio >= 1.22:
        return 2
    if line.bold and ratio >= 1.05 and len(text) <= 48:
        return 3
    return None


def _is_heading_role_noise(
    line: _PdfLine,
    *,
    page_number: int,
    page_height: float,
    body_font_size: float,
) -> bool:
    text = _normalize_text(line.text)
    if _looks_like_figure_legend(text):
        return True
    if (
        CHAPTER_PATTERN.match(text)
        or NUMBERED_HEADING_PATTERN.match(text)
        or CHINESE_HEADING_PATTERN.match(text)
    ):
        return False
    if _looks_like_author_line(text):
        return True
    if (
        page_number == 1
        and line.bbox.y0 <= page_height * 0.55
        and line.font_size <= body_font_size * 1.25
        and _latin_character_ratio(text) >= 0.6
    ):
        return True
    return False


def _looks_like_figure_legend(text: str) -> bool:
    markers = re.findall(
        r"(?:^|[；;])\s*\d{1,2}[.．、]",
        str(text or ""),
    )
    return len(markers) >= 2


def _looks_like_author_line(text: str) -> bool:
    value = str(text or "").strip()
    if len(value) > 120 or not any(character.isdigit() for character in value):
        return False
    if value.count(",") + value.count("，") < 2:
        return False
    latin_words = re.findall(r"[A-Za-z]{2,}", value)
    return len(latin_words) >= 3


def _latin_character_ratio(text: str) -> float:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return 0.0
    latin = sum(character.isascii() and character.isalpha() for character in compact)
    return latin / len(compact)


def _is_non_heading(line: _PdfLine) -> bool:
    text = line.text.strip()
    compact = re.sub(r"\s+", "", text)
    if line.is_math or _looks_like_formula_text(text):
        return True
    if _is_figure_caption(text) or _is_table_caption(text):
        return True
    if DOI_PATTERN.search(text):
        return True
    if any(
        marker in compact.casefold()
        for marker in (
            "doi:",
            "abstract",
            "关键词",
            "作者简介",
            "通信作者",
        )
    ):
        return True
    if re.fullmatch(r"[-+±\d.·:：/％%()（）\[\]]+", compact):
        return True
    digit_count = sum(character.isdigit() for character in compact)
    if digit_count / max(len(compact), 1) > 0.65:
        return True
    if _looks_like_sentence(text):
        return True
    return False


def _looks_like_formula_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if any(marker in compact for marker in ("=", "＝", "∑", "√", "∫", "≈", "≤", "≥")):
        return True
    if re.search(
        r"\b(?:sqrt|sum|log|ln|exp|min|max|mean|var)\s*\(",
        str(text or ""),
        re.IGNORECASE,
    ):
        return True
    operators = sum(character in "+-−*/×÷^_" for character in compact)
    variables = sum(character.isalpha() for character in compact)
    digits = sum(character.isdigit() for character in compact)
    cjk = sum("\u4e00" <= character <= "\u9fff" for character in compact)
    return (
        len(compact) <= 80
        and cjk == 0
        and operators >= 2
        and variables >= 1
        and digits >= 1
        and any(marker in compact for marker in ("(", ")", "/", "^", "_"))
    )


def _looks_like_sentence(text: str) -> bool:
    value = _normalize_text(text)
    compact = re.sub(r"\s+", "", value)
    if len(compact) > 48:
        return True
    if len(compact) > 28 and any(mark in value for mark in "。！？；，,.;!?"):
        return True
    words = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", value)
    return len(words) > 10


def _updated_section_stack(
    stack: list[tuple[int, str]],
    level: int,
    text: str,
) -> list[tuple[int, str]]:
    result = [item for item in stack if item[0] < level]
    result.append((level, text))
    return result


def _is_margin_noise(
    line: _PdfLine,
    page_height: float,
    repeated_margin_keys: set[str],
) -> bool:
    if not _is_in_margin(line.bbox, page_height):
        return False
    if PAGE_NUMBER_PATTERN.fullmatch(line.text.strip()):
        return True
    return _normalize_margin_text(line.text) in repeated_margin_keys


def _is_in_margin(bbox: BoundingBox, page_height: float) -> bool:
    return bbox.y1 <= page_height * MARGIN_RATIO or bbox.y0 >= page_height * (
        1 - MARGIN_RATIO
    )


def _normalize_margin_text(text: str) -> str:
    normalized = _normalize_text(text).casefold()
    return re.sub(r"\d+", "#", normalized)


def _bbox_overlap_ratio(first: BoundingBox, second: BoundingBox) -> float:
    x0 = max(first.x0, second.x0)
    y0 = max(first.y0, second.y0)
    x1 = min(first.x1, second.x1)
    y1 = min(first.y1, second.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    overlap = (x1 - x0) * (y1 - y0)
    area = max((first.x1 - first.x0) * (first.y1 - first.y0), 1.0)
    return overlap / area


def _bbox_area(bbox: BoundingBox) -> float:
    return max(bbox.x1 - bbox.x0, 0.0) * max(bbox.y1 - bbox.y0, 0.0)


def _union_bbox(boxes: Sequence[BoundingBox]) -> BoundingBox:
    return BoundingBox(
        min(box.x0 for box in boxes),
        min(box.y0 for box in boxes),
        max(box.x1 for box in boxes),
        max(box.y1 for box in boxes),
    )


def _weighted_median(weights: Counter[float]) -> float:
    if not weights:
        return 0.0
    midpoint = sum(weights.values()) / 2
    cumulative = 0
    for value, weight in sorted(weights.items()):
        cumulative += weight
        if cumulative >= midpoint:
            return float(value)
    return float(max(weights))


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _meaningful_char_count(value: str) -> int:
    return sum(
        character.isalnum() or "\u4e00" <= character <= "\u9fff"
        for character in str(value or "")
    )


def _mean(values: Any) -> float:
    items = [float(value) for value in values]
    return sum(items) / len(items) if items else 0.0
