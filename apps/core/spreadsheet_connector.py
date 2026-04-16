import csv
from datetime import date
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


def _project_rows(rows, output_headers=None, column_map=None, default_values=None):
    if not output_headers:
        return rows

    source_header = rows[0] if rows else []
    source_index = {_normalize_text(value): index for index, value in enumerate(source_header)}
    column_map = column_map or {}
    default_values = default_values or {}

    projected_rows = [output_headers]
    for row in rows[1:]:
        projected_row = []
        for output_header in output_headers:
            source_reference = column_map.get(output_header, output_header)
            if isinstance(source_reference, int):
                source_idx = source_reference
            else:
                source_idx = source_index.get(_normalize_text(source_reference))
            if source_idx is None:
                projected_row.append(default_values.get(output_header, ""))
                continue
            value = row[source_idx] if source_idx < len(row) else ""
            if value == "":
                value = default_values.get(output_header, "")
            projected_row.append(value)
        projected_rows.append(projected_row)
    return projected_rows


def _split_value(value, delimiter):
    if delimiter not in value:
        return value.strip(), ""
    left, right = value.split(delimiter, 1)
    return left.strip(), right.strip()


def _monday_for_iso_week(year_value, week_value):
    if year_value in ("", None) or week_value in ("", None):
        return ""
    try:
        year = int(str(year_value).strip())
        week = int(str(week_value).strip())
    except ValueError:
        return ""
    return date.fromisocalendar(year, week, 1).isoformat()


def _value_for_transform(source_name, transformed_row, transformed_index, source_row, source_index):
    projected_idx = transformed_index.get(_normalize_text(source_name))
    if projected_idx is not None:
        return transformed_row[projected_idx]
    original_idx = source_index.get(_normalize_text(source_name))
    if original_idx is not None and original_idx < len(source_row):
        return source_row[original_idx]
    return ""


def _apply_row_transforms(rows, row_transforms=None, source_rows=None):
    if not row_transforms:
        return rows

    output_headers = rows[0] if rows else []
    transformed_index = {_normalize_text(value): index for index, value in enumerate(output_headers)}
    source_headers = source_rows[0] if source_rows else []
    source_index = {_normalize_text(value): index for index, value in enumerate(source_headers)}
    transformed_rows = [output_headers]

    for row_number, row in enumerate(rows[1:], start=1):
        transformed_row = list(row)
        source_row = source_rows[row_number] if source_rows and row_number < len(source_rows) else []
        for transform in row_transforms:
            transform_type = transform["type"]
            if transform_type == "split":
                left_value, right_value = _split_value(
                    _value_for_transform(
                        transform["source"],
                        transformed_row,
                        transformed_index,
                        source_row,
                        source_index,
                    ),
                    transform.get("delimiter", "//"),
                )
                left_target = transformed_index.get(_normalize_text(transform["left_target"]))
                right_target = transformed_index.get(_normalize_text(transform["right_target"]))
                if left_target is not None:
                    transformed_row[left_target] = left_value
                if right_target is not None:
                    transformed_row[right_target] = right_value
            elif transform_type == "copy":
                value = _value_for_transform(
                    transform["source"],
                    transformed_row,
                    transformed_index,
                    source_row,
                    source_index,
                )
                for target in transform.get("targets", []):
                    target_index = transformed_index.get(_normalize_text(target))
                    if target_index is not None:
                        transformed_row[target_index] = value
            elif transform_type == "week_monday":
                target_index = transformed_index.get(_normalize_text(transform["target"]))
                if target_index is None:
                    continue
                transformed_row[target_index] = _monday_for_iso_week(
                    _value_for_transform(
                        transform["year_source"],
                        transformed_row,
                        transformed_index,
                        source_row,
                        source_index,
                    ),
                    _value_for_transform(
                        transform["week_source"],
                        transformed_row,
                        transformed_index,
                        source_row,
                        source_index,
                    ),
                )
        transformed_rows.append(transformed_row)

    return transformed_rows


def _truncate_source_rows(source_rows, stop_on_blank_in=None):
    if not stop_on_blank_in or not source_rows:
        return source_rows

    header = source_rows[0]
    rows = source_rows[1:]
    header_index = {_normalize_text(value): index for index, value in enumerate(header)}
    stop_indexes = []
    for header_name in stop_on_blank_in:
        index = header_index.get(_normalize_text(header_name))
        if index is not None:
            stop_indexes.append(index)

    if not stop_indexes:
        return source_rows

    truncated_rows = [header]
    for row in rows:
        should_stop = False
        for index in stop_indexes:
            value = row[index] if index < len(row) else ""
            if value == "":
                should_stop = True
                break
        if should_stop:
            break
        truncated_rows.append(row)
    return truncated_rows


def _normalize_single_region(
    rows,
    required_headers,
    aliases=None,
    max_scan_rows=200,
    anchor_token=None,
    header_row_index=None,
    output_headers=None,
    column_map=None,
    default_values=None,
    row_transforms=None,
    stop_on_blank_in=None,
    prefer_anchor_token=False,
):
    header_index, canonical_header, strategy = detect_header_row(
        rows,
        required_headers=required_headers,
        aliases=aliases,
        max_scan_rows=max_scan_rows,
        anchor_token=anchor_token,
        header_row_index=header_row_index,
        prefer_anchor_token=prefer_anchor_token,
    )
    source_rows = [canonical_header] + rows[header_index + 1 :]
    source_rows = _truncate_source_rows(source_rows, stop_on_blank_in=stop_on_blank_in)
    normalized_rows = _project_rows(
        source_rows,
        output_headers=output_headers,
        column_map=column_map,
        default_values=default_values,
    )
    normalized_rows = _apply_row_transforms(
        normalized_rows,
        row_transforms=row_transforms,
        source_rows=source_rows,
    )
    return {
        "header_row_index": header_index,
        "strategy": strategy,
        "rows": normalized_rows,
    }


def detect_header_row(
    rows,
    required_headers,
    aliases=None,
    max_scan_rows=200,
    anchor_token=None,
    header_row_index=None,
    prefer_anchor_token=False,
):
    normalized_required = {_normalize_text(value) for value in required_headers}
    scan_limit = min(len(rows), max_scan_rows)

    if anchor_token and prefer_anchor_token:
        normalized_anchor = _normalize_text(anchor_token)
        for index in range(scan_limit):
            first_cell = rows[index][0] if rows[index] else ""
            if normalized_anchor in _normalize_text(first_cell):
                candidate_index = index + 1
                if candidate_index < len(rows):
                    canonical = canonicalize_header_row(rows[candidate_index], aliases=aliases)
                    return candidate_index, canonical, "anchor_token"

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
    output_headers=None,
    column_map=None,
    default_values=None,
    row_transforms=None,
    source_regions=None,
    stop_on_blank_in=None,
    prefer_anchor_token=False,
):
    if not source_regions:
        return _normalize_single_region(
            rows,
            required_headers=required_headers,
            aliases=aliases,
            max_scan_rows=max_scan_rows,
            anchor_token=anchor_token,
            header_row_index=header_row_index,
            output_headers=output_headers,
            column_map=column_map,
            default_values=default_values,
            row_transforms=row_transforms,
            stop_on_blank_in=stop_on_blank_in,
            prefer_anchor_token=prefer_anchor_token,
        )

    region_results = []
    for region in source_regions:
        region_results.append(
            _normalize_single_region(
                rows,
                required_headers=region.get("required_headers", required_headers),
                aliases=region.get("aliases", aliases),
                max_scan_rows=region.get("max_scan_rows", max_scan_rows),
                anchor_token=region.get("anchor_token", anchor_token),
                header_row_index=region.get("header_row_index", header_row_index),
                output_headers=region.get("output_headers", output_headers),
                column_map=region.get("column_map", column_map),
                default_values=region.get("default_values", default_values),
                row_transforms=region.get("row_transforms", row_transforms),
                stop_on_blank_in=region.get("stop_on_blank_in", stop_on_blank_in),
                prefer_anchor_token=region.get("prefer_anchor_token", prefer_anchor_token),
            )
        )

    first_rows = region_results[0]["rows"]
    merged_rows = [first_rows[0]]
    for result in region_results:
        rows_without_header = result["rows"][1:]
        merged_rows.extend(rows_without_header)

    return {
        "header_row_index": region_results[0]["header_row_index"],
        "header_row_indexes": [result["header_row_index"] for result in region_results],
        "strategy": "multi_region",
        "rows": merged_rows,
    }


def normalize_csv_file(
    source_path,
    output_path,
    required_headers,
    aliases=None,
    max_scan_rows=200,
    anchor_token=None,
    header_row_index=None,
    output_headers=None,
    column_map=None,
    default_values=None,
    row_transforms=None,
    source_regions=None,
    stop_on_blank_in=None,
    prefer_anchor_token=False,
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
        output_headers=output_headers,
        column_map=column_map,
        default_values=default_values,
        row_transforms=row_transforms,
        source_regions=source_regions,
        stop_on_blank_in=stop_on_blank_in,
        prefer_anchor_token=prefer_anchor_token,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(normalized["rows"])

    normalized["rows_written"] = max(len(normalized["rows"]) - 1, 0)
    return normalized
