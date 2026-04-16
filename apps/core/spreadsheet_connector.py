import csv
from pathlib import Path


def _normalize_text(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).casefold()


def canonicalize_header_row(header_row, aliases=None):
    alias_map = {_normalize_text(key): value for key, value in (aliases or {}).items()}
    canonical = []
    for cell in header_row:
        normalized = _normalize_text(cell)
        canonical.append(alias_map.get(normalized, " ".join(str(cell).strip().split())))
    return canonical


def detect_header_row(
    rows,
    required_headers,
    aliases=None,
    max_scan_rows=200,
    anchor_token=None,
    header_row_index=None,
):
    normalized_required = {_normalize_text(value) for value in required_headers}
    scan_limit = min(len(rows), max_scan_rows)

    for index in range(scan_limit):
        canonical = canonicalize_header_row(rows[index], aliases=aliases)
        if normalized_required <= {_normalize_text(value) for value in canonical}:
            return index, canonical, "required_header_set_scan"

    if anchor_token:
        normalized_anchor = _normalize_text(anchor_token)
        for index in range(scan_limit):
            first_cell = rows[index][0] if rows[index] else ""
            if normalized_anchor in _normalize_text(first_cell):
                candidate_index = index + 1
                if candidate_index < len(rows):
                    canonical = canonicalize_header_row(rows[candidate_index], aliases=aliases)
                    return candidate_index, canonical, "anchor_token"

    if header_row_index is not None:
        if header_row_index < 0 or header_row_index >= len(rows):
            raise ValueError(f"header_row_index {header_row_index} is out of range")
        canonical = canonicalize_header_row(rows[header_row_index], aliases=aliases)
        return header_row_index, canonical, "header_row_index"

    raise ValueError(
        f"no header row matching contract found in first {scan_limit} rows"
    )


def normalize_rows(
    rows,
    required_headers,
    aliases=None,
    max_scan_rows=200,
    anchor_token=None,
    header_row_index=None,
):
    header_index, canonical_header, strategy = detect_header_row(
        rows,
        required_headers=required_headers,
        aliases=aliases,
        max_scan_rows=max_scan_rows,
        anchor_token=anchor_token,
        header_row_index=header_row_index,
    )
    return {
        "header_row_index": header_index,
        "strategy": strategy,
        "rows": [canonical_header] + rows[header_index + 1 :],
    }


def normalize_csv_file(
    source_path,
    output_path,
    required_headers,
    aliases=None,
    max_scan_rows=200,
    anchor_token=None,
    header_row_index=None,
):
    with Path(source_path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    normalized = normalize_rows(
        rows,
        required_headers=required_headers,
        aliases=aliases,
        max_scan_rows=max_scan_rows,
        anchor_token=anchor_token,
        header_row_index=header_row_index,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(normalized["rows"])

    normalized["rows_written"] = max(len(normalized["rows"]) - 1, 0)
    return normalized
