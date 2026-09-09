
from __future__ import annotations

import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Number = float
BBox = Tuple[float, float, float, float]


def _require_gdstk():
    try:
        import gdstk
        return gdstk
    except ImportError as exc:
        raise ImportError(
            "The gdstk package is required to read GDSII files. Install it with:\n\n"
            "    python -m pip install -r requirements.txt\n\n"
            "or:\n\n"
            "    python -m pip install gdstk matplotlib\n"
        ) from exc


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _point_tuple(value: Any) -> Tuple[float, float]:
    if value is None:
        return (0.0, 0.0)
    try:
        return (_as_float(value[0]), _as_float(value[1]))
    except Exception:
        return (0.0, 0.0)


def _sequence_or_empty(value: Any) -> Any:
    if value is None:
        return []
    return value


def _safe_len(value: Any) -> int:
    if value is None:
        return 0
    try:
        return len(value)
    except Exception:
        return 0


def _bbox_from_points(points: Any) -> Optional[BBox]:
    try:
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
    except Exception:
        return None
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_from_object(obj: Any) -> Optional[BBox]:
    try:
        bbox = obj.bounding_box()
    except Exception:
        bbox = None

    if bbox is not None:
        try:
            return (
                float(bbox[0][0]),
                float(bbox[0][1]),
                float(bbox[1][0]),
                float(bbox[1][1]),
            )
        except Exception:
            pass

    points = getattr(obj, "points", None)
    if points is not None:
        return _bbox_from_points(points)
    return None


def _bbox_to_record(prefix: str, bbox: Optional[BBox]) -> Dict[str, Any]:
    if bbox is None:
        return {
            f"{prefix}_min_x_um": "",
            f"{prefix}_min_y_um": "",
            f"{prefix}_max_x_um": "",
            f"{prefix}_max_y_um": "",
            f"{prefix}_width_um": "",
            f"{prefix}_height_um": "",
            f"{prefix}_center_x_um": "",
            f"{prefix}_center_y_um": "",
        }
    min_x, min_y, max_x, max_y = bbox
    return {
        f"{prefix}_min_x_um": min_x,
        f"{prefix}_min_y_um": min_y,
        f"{prefix}_max_x_um": max_x,
        f"{prefix}_max_y_um": max_y,
        f"{prefix}_width_um": max_x - min_x,
        f"{prefix}_height_um": max_y - min_y,
        f"{prefix}_center_x_um": (min_x + max_x) / 2.0,
        f"{prefix}_center_y_um": (min_y + max_y) / 2.0,
    }


def _bbox_size(bbox: BBox) -> Tuple[float, float]:
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def _bbox_center(bbox: BBox) -> Tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _inside_bbox(x: float, y: float, bbox: BBox, margin: float = 0.0) -> bool:
    return (bbox[0] - margin) <= x <= (bbox[2] + margin) and (bbox[1] - margin) <= y <= (bbox[3] + margin)


def _safe_cell_name(cell_or_name: Any) -> str:
    if cell_or_name is None:
        return ""
    return str(getattr(cell_or_name, "name", cell_or_name))


def _cell_dict(lib: Any) -> Dict[str, Any]:
    return {cell.name: cell for cell in getattr(lib, "cells", [])}


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


def parse_name_list(value: Optional[Any]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).replace(";", ",").replace("\n", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _reference_target_name(ref: Any) -> str:
    try:
        return str(ref.cell.name)
    except Exception:
        return str(getattr(ref, "cell", getattr(ref, "cell_name", "")))


def _reference_target_cell(ref: Any) -> Any:
    try:
        cell = ref.cell
        if hasattr(cell, "name"):
            return cell
    except Exception:
        pass
    return None


Transform = Tuple[float, float, float, float, float, float]


def _identity_transform() -> Transform:
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _reference_transform(ref: Any) -> Transform:
    ox, oy = _point_tuple(getattr(ref, "origin", None))
    rot = _as_float(getattr(ref, "rotation", 0.0), 0.0)
    mag_raw = getattr(ref, "magnification", 1.0)
    mag = 1.0 if mag_raw in (None, "") else _as_float(mag_raw, 1.0)
    reflect = bool(getattr(ref, "x_reflection", False))

    cos_r = math.cos(rot)
    sin_r = math.sin(rot)

    y_sign = -1.0 if reflect else 1.0
    a = mag * cos_r
    b = -mag * sin_r * y_sign
    c = mag * sin_r
    d = mag * cos_r * y_sign
    return (a, b, c, d, ox, oy)


def _compose_transform(parent: Transform, child: Transform) -> Transform:
    pa, pb, pc, pd, ptx, pty = parent
    ca, cb, cc, cd, ctx, cty = child
    return (
        pa * ca + pb * cc,
        pa * cb + pb * cd,
        pc * ca + pd * cc,
        pc * cb + pd * cd,
        pa * ctx + pb * cty + ptx,
        pc * ctx + pd * cty + pty,
    )


def _apply_transform(transform: Transform, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, tx, ty = transform
    return (a * x + b * y + tx, c * x + d * y + ty)


def _transform_bbox(bbox: Optional[BBox], transform: Transform) -> Optional[BBox]:
    if bbox is None:
        return None
    min_x, min_y, max_x, max_y = bbox
    pts = [
        _apply_transform(transform, min_x, min_y),
        _apply_transform(transform, min_x, max_y),
        _apply_transform(transform, max_x, min_y),
        _apply_transform(transform, max_x, max_y),
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _name_matches(candidate: str, wanted_names: Sequence[str]) -> Tuple[bool, str]:
    candidate_norm = _normalize_name(candidate)
    for wanted in wanted_names:
        wanted_norm = _normalize_name(wanted)
        if not wanted_norm:
            continue
        if wanted_norm.endswith("*"):
            if candidate_norm.startswith(wanted_norm[:-1]):
                return True, wanted
        elif candidate_norm == wanted_norm or wanted_norm in candidate_norm:
            return True, wanted
    return False, ""


def default_alignment_mark_names() -> str:
    return "ATA_ALIGN_*, KS_LYR4, KS_Neg_LYR2"


def _is_ata_name(value: Any) -> bool:
    return str(value or "").strip().upper().startswith("ATA_")


def _classify_ata_name(value: Any) -> str:
    name = str(value or "").strip().upper()
    if name.startswith("ATA_ALIGN_"):
        return "alignment_mark"
    if name.startswith("ATA_DIE_ORIGIN"):
        return "die_origin"
    if name.startswith("ATA_DIE_BOUNDARY"):
        return "die_boundary"
    if name.startswith("ATA_DEVICE_BOUNDARY"):
        return "device_boundary"
    if name.startswith("ATA_RETICLE_ORIGIN"):
        return "reticle_origin"
    if name.startswith("ATA_WAFER_ORIGIN"):
        return "wafer_origin"
    if name.startswith("ATA_SCRIBE") or name.startswith("ATA_STREET"):
        return "scribe_or_street"
    if name.startswith("ATA_PAD_"):
        return "probe_pad_label"
    if name.startswith("ATA_DEVICE_"):
        return "device"
    if name.startswith("ATA_TEST_"):
        return "test_structure"
    if name.startswith("ATA_SITE_"):
        return "probe_site"
    if name.startswith("ATA_NANOZ_"):
        return "nanoz_channel"
    if name.startswith("ATA_CHANNEL_"):
        return "instrument_channel"
    if name.startswith("ATA_"):
        return "ata_metadata"
    return ""


def _ata_record_from_label(label: Any, cell_name: str, path: str, index: int, transform: Transform) -> Optional[Dict[str, Any]]:
    text = str(getattr(label, "text", "")).strip()
    record_class = _classify_ata_name(text)
    if not record_class:
        return None
    lx, ly = _point_tuple(getattr(label, "origin", None))
    x_um, y_um = _apply_transform(transform, lx, ly)
    return {
        "ata_id": f"ATA{index:04d}",
        "name": text,
        "record_class": record_class,
        "source": "gds_text_label",
        "parent_cell": cell_name,
        "occurrence_path": f"{path}/TEXT:{text}[{index}]",
        "x_um": x_um,
        "y_um": y_um,
        "layer": getattr(label, "layer", ""),
        "datatype_or_texttype": getattr(label, "texttype", getattr(label, "datatype", "")),
        "rotation": getattr(label, "rotation", ""),
        "magnification": getattr(label, "magnification", ""),
        "x_reflection": getattr(label, "x_reflection", ""),
        "bbox_min_x_um": "",
        "bbox_min_y_um": "",
        "bbox_max_x_um": "",
        "bbox_max_y_um": "",
        "bbox_width_um": "",
        "bbox_height_um": "",
        "notes": "Detected from ATA_ text label. Label origin is treated as the coordinate anchor.",
    }


def _ata_record_from_reference(ref: Any, target_name: str, target_cell: Any, parent_cell_name: str, path: str, index: int, transform: Transform) -> Optional[Dict[str, Any]]:
    record_class = _classify_ata_name(target_name)
    if not record_class:
        return None
    target_bbox = _bbox_from_object(target_cell) if target_cell is not None else _bbox_from_object(ref)
    abs_bbox = _transform_bbox(target_bbox, transform)
    if abs_bbox is not None:
        x_um, y_um = _bbox_center(abs_bbox)
    else:
        x_um, y_um = _apply_transform(transform, 0.0, 0.0)
    rec: Dict[str, Any] = {
        "ata_id": f"ATA{index:04d}",
        "name": target_name,
        "record_class": record_class,
        "source": "gds_reference_cell_name",
        "parent_cell": parent_cell_name,
        "occurrence_path": f"{path}/{target_name}[{index}]",
        "x_um": x_um,
        "y_um": y_um,
        "layer": "",
        "datatype_or_texttype": "",
        "rotation": getattr(ref, "rotation", ""),
        "magnification": getattr(ref, "magnification", ""),
        "x_reflection": getattr(ref, "x_reflection", ""),
        "notes": "Detected from ATA_ referenced cell name. BBox center is used when available; otherwise reference origin is used.",
    }
    rec.update(_bbox_to_record("bbox", abs_bbox))
    return rec


def extract_ata_convention_records(
    selected_cell: Any,
    selected_cell_name: str,
    max_depth: int = 30,
) -> Dict[str, List[Dict[str, Any]]]:
    all_records: List[Dict[str, Any]] = []
    if selected_cell is None:
        return {
            "ata_metadata": [],
            "ata_die_markers": [],
            "ata_devices": [],
            "ata_test_structures": [],
            "ata_sites": [],
            "ata_channel_map": [],
        }

    def add_record(rec: Optional[Dict[str, Any]]) -> None:
        if rec is None:
            return
        rec["ata_id"] = f"ATA{len(all_records) + 1:04d}"
        all_records.append(rec)

    def visit(cell: Any, transform: Transform, path: str, stack: List[str], depth: int) -> None:
        if cell is None or depth > max_depth:
            return
        cell_name = _safe_cell_name(cell)
        if cell_name in stack:
            return
        next_stack = stack + [cell_name]

        for label in iter_cell_labels(cell):
            add_record(_ata_record_from_label(label, cell_name, path, len(all_records) + 1, transform))

        for ref in iter_cell_references(cell):
            target_name = _reference_target_name(ref)
            target_cell = _reference_target_cell(ref)
            total_transform = _compose_transform(transform, _reference_transform(ref))
            add_record(_ata_record_from_reference(ref, target_name, target_cell, cell_name, path, len(all_records) + 1, total_transform))
            if target_cell is not None:
                visit(target_cell, total_transform, f"{path}/{target_name}", next_stack, depth + 1)

    visit(selected_cell, _identity_transform(), selected_cell_name, [], 0)

    die_classes = {"die_origin", "die_boundary", "device_boundary", "reticle_origin", "wafer_origin", "scribe_or_street"}
    device_classes = {"device"}
    test_classes = {"test_structure"}
    site_classes = {"probe_site", "nanoz_channel", "instrument_channel"}

    die_markers = [r for r in all_records if r.get("record_class") in die_classes]
    devices = [r for r in all_records if r.get("record_class") in device_classes]
    test_structures = [r for r in all_records if r.get("record_class") in test_classes]
    sites = [r for r in all_records if r.get("record_class") in site_classes]
    channel_map = []
    for r in all_records:
        if r.get("record_class") in {"probe_pad_label", "probe_site", "nanoz_channel", "instrument_channel"}:
            channel_map.append(
                {
                    "map_id": f"MAP{len(channel_map) + 1:04d}",
                    "source_name": r.get("name", ""),
                    "record_class": r.get("record_class", ""),
                    "x_um": r.get("x_um", ""),
                    "y_um": r.get("y_um", ""),
                    "parent_cell": r.get("parent_cell", ""),
                    "occurrence_path": r.get("occurrence_path", ""),
                    "instrument": "",
                    "board_id": "",
                    "channel": "",
                    "site": "",
                    "net_name": "",
                    "notes": "Fill in board/channel assignments during probe-card mapping.",
                }
            )

    return {
        "ata_metadata": all_records,
        "ata_die_markers": die_markers,
        "ata_devices": devices,
        "ata_test_structures": test_structures,
        "ata_sites": sites,
        "ata_channel_map": channel_map,
    }


def discover_layout_metadata(gds_path: str) -> Tuple[Dict[str, Any], str]:
    if not gds_path:
        return {}, ""
    base, _ext = os.path.splitext(gds_path)
    candidates = [
        base + ".json",
        base + "_metadata.json",
        base + "_ata_metadata.json",
        os.path.join(os.path.dirname(gds_path), "layout_metadata.json"),
        os.path.join(os.path.dirname(gds_path), "ata_layout_metadata.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f), path
            except Exception:
                return {}, path
    return {}, ""


def _metadata_value(metadata: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not metadata:
        return default
    for key in keys:
        if key in metadata:
            return metadata[key]
    for section in ("wafer", "die", "layers", "probe", "ata", "layout"):
        sub = metadata.get(section)
        if isinstance(sub, dict):
            for key in keys:
                if key in sub:
                    return sub[key]
    return default


def metadata_to_records(metadata: Dict[str, Any], metadata_path: str = "") -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not metadata:
        return records
    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        else:
            records.append({"metadata_key": prefix, "metadata_value": _json_safe(value), "source_file": metadata_path})
    walk("", metadata)
    return records


def validate_ata_convention(
    data: Dict[str, Any],
    layout_metadata: Optional[Dict[str, Any]] = None,
    metadata_path: str = "",
) -> List[Dict[str, Any]]:
    metadata = layout_metadata or {}
    records: List[Dict[str, Any]] = []

    def add(check: str, status: str, detail: str, recommendation: str = "") -> None:
        records.append(
            {
                "check_id": f"CHK{len(records) + 1:03d}",
                "check": check,
                "status": status,
                "detail": detail,
                "recommendation": recommendation,
            }
        )

    summary = data.get("summary", {})
    alignment_count = len(data.get("alignment_marks", []))
    ata_count = len(data.get("ata_metadata", []))
    die_origin_count = len([r for r in data.get("ata_die_markers", []) if r.get("record_class") == "die_origin"])
    die_boundary_count = len([r for r in data.get("ata_die_markers", []) if r.get("record_class") in {"die_boundary", "device_boundary"}])
    ata_pad_label_count = len([r for r in data.get("ata_metadata", []) if r.get("record_class") == "probe_pad_label"])
    pad_count = len(data.get("pads", []))
    device_count = len(data.get("ata_devices", []))
    test_count = len(data.get("ata_test_structures", []))

    if ata_count:
        add("ATA naming convention", "PASS", f"Detected {ata_count} ATA_* labels/references.")
    else:
        add("ATA naming convention", "WARN", "No ATA_* labels/references detected.", "Ask layout to add ATA_* labels/cells per the manual.")

    if alignment_count >= 2:
        add("Alignment marks", "PASS", f"Detected {alignment_count} alignment marks.")
    elif alignment_count == 1:
        add("Alignment marks", "WARN", "Only one alignment mark was detected.", "Use at least two far-apart marks; three is preferred.")
    else:
        add("Alignment marks", "FAIL", "No alignment marks were detected.", "Add ATA_ALIGN_* cells/labels or update the search names.")

    if die_origin_count:
        add("Die origin", "PASS", f"Detected {die_origin_count} ATA_DIE_ORIGIN marker(s).")
    else:
        add("Die origin", "WARN", "No ATA_DIE_ORIGIN marker detected.", "Place an ATA_DIE_ORIGIN text label at the intended die coordinate origin.")

    if die_boundary_count or summary.get("selected_cell_bbox"):
        source = "ATA boundary marker" if die_boundary_count else "selected cell bounding box fallback"
        add("Die/device boundary", "PASS", f"Boundary information available from {source}.")
    else:
        add("Die/device boundary", "WARN", "No boundary information detected.", "Add ATA_DIE_BOUNDARY or ATA_DEVICE_BOUNDARY and verify die extents.")

    if ata_pad_label_count or pad_count:
        add("Probe pad extraction", "PASS", f"Detected {pad_count} pad geometry candidates and {ata_pad_label_count} ATA_PAD_* labels.")
    else:
        add("Probe pad extraction", "WARN", "No pad candidates or ATA_PAD_* labels detected.", "Provide pad layer/datatype and label pads with ATA_PAD_<name>.")

    if device_count or test_count:
        add("Device/test structure labels", "PASS", f"Detected {device_count} ATA_DEVICE_* and {test_count} ATA_TEST_* records.")
    else:
        add("Device/test structure labels", "WARN", "No ATA_DEVICE_* or ATA_TEST_* labels detected.", "Add labels to make future test-plan generation automatic.")

    if metadata:
        add("Sidecar metadata JSON", "PASS", f"Loaded metadata from {metadata_path or 'provided metadata'}." )
    else:
        add("Sidecar metadata JSON", "WARN", "No layout metadata JSON was loaded.", "Provide <gds_basename>_metadata.json or ata_layout_metadata.json with wafer/die/layer definitions.")

    required_metadata = ["wafer_diameter_mm", "die_pitch_x_um", "die_pitch_y_um", "origin_definition", "pad_layer"]
    missing = [k for k in required_metadata if _metadata_value(metadata, k) is None]
    if not missing:
        add("Required metadata fields", "PASS", "Required wafer/die/layer metadata fields are present.")
    else:
        add("Required metadata fields", "WARN", "Missing metadata fields: " + ", ".join(missing), "Add the missing fields to the sidecar metadata JSON.")

    return records

def extract_alignment_marks(
    selected_cell: Any,
    selected_cell_name: str,
    alignment_mark_names: Optional[Any] = None,
    max_depth: int = 30,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    wanted_names = parse_name_list(alignment_mark_names) or parse_name_list(default_alignment_mark_names())
    if selected_cell is None or not wanted_names:
        return [], []

    found: List[Dict[str, Any]] = []

    def visit(cell: Any, transform: Transform, path: str, stack: List[str], depth: int) -> None:
        if cell is None or depth > max_depth:
            return
        cell_name = _safe_cell_name(cell)
        if cell_name in stack:
            return
        next_stack = stack + [cell_name]

        for i, ref in enumerate(iter_cell_references(cell), start=1):
            target_name = _reference_target_name(ref)
            target_cell = _reference_target_cell(ref)
            ref_transform = _reference_transform(ref)
            total_transform = _compose_transform(transform, ref_transform)
            occurrence_path = f"{path}/{target_name}[{i}]"

            match, matched_name = _name_matches(target_name, wanted_names)
            if match:
                target_bbox = _bbox_from_object(target_cell) if target_cell is not None else None
                abs_bbox = _transform_bbox(target_bbox, total_transform)
                if abs_bbox is not None:
                    x_um, y_um = _bbox_center(abs_bbox)
                else:
                    x_um, y_um = _apply_transform(transform, *_point_tuple(getattr(ref, "origin", None)))

                mark_id = f"ALIGN{len(found) + 1:03d}"
                rec: Dict[str, Any] = {
                    "mark_id": mark_id,
                    "mark_name": target_name,
                    "matched_search_name": matched_name,
                    "parent_cell": cell_name,
                    "reference_index": i,
                    "occurrence_path": occurrence_path,
                    "x_um": x_um,
                    "y_um": y_um,
                    "source": "gds_reference_cell_name",
                    "rotation": getattr(ref, "rotation", ""),
                    "magnification": getattr(ref, "magnification", ""),
                    "x_reflection": getattr(ref, "x_reflection", ""),
                    "notes": "Auto-detected from alignment mark cell/reference name",
                }
                rec.update(_bbox_to_record("bbox", abs_bbox))
                found.append(rec)

            if target_cell is not None:
                visit(target_cell, total_transform, occurrence_path, next_stack, depth + 1)

        for j, label in enumerate(iter_cell_labels(cell), start=1):
            label_text = str(getattr(label, "text", "")).strip()
            match, matched_name = _name_matches(label_text, wanted_names)
            if not match:
                continue
            lx, ly = _point_tuple(getattr(label, "origin", None))
            x_um, y_um = _apply_transform(transform, lx, ly)
            mark_id = f"ALIGN{len(found) + 1:03d}"
            found.append(
                {
                    "mark_id": mark_id,
                    "mark_name": label_text,
                    "matched_search_name": matched_name,
                    "parent_cell": cell_name,
                    "reference_index": "",
                    "occurrence_path": f"{path}/TEXT:{label_text}[{j}]",
                    "x_um": x_um,
                    "y_um": y_um,
                    "source": "gds_text_label",
                    "rotation": getattr(label, "rotation", ""),
                    "magnification": getattr(label, "magnification", ""),
                    "x_reflection": getattr(label, "x_reflection", ""),
                    "bbox_min_x_um": "",
                    "bbox_min_y_um": "",
                    "bbox_max_x_um": "",
                    "bbox_max_y_um": "",
                    "bbox_width_um": "",
                    "bbox_height_um": "",
                    "bbox_center_x_um": x_um,
                    "bbox_center_y_um": y_um,
                    "notes": "Auto-detected from alignment mark text label",
                }
            )

    visit(selected_cell, _identity_transform(), selected_cell_name, [], 0)

    ata_alignment_marks: List[Dict[str, Any]] = []
    for mark in found:
        ata_alignment_marks.append(
            {
                "mark_id": mark.get("mark_id", ""),
                "mark_name": mark.get("mark_name", ""),
                "x_um": mark.get("x_um", ""),
                "y_um": mark.get("y_um", ""),
                "source": mark.get("source", ""),
                "gds_parent_cell": mark.get("parent_cell", ""),
                "occurrence_path": mark.get("occurrence_path", ""),
                "alignment_role": "",
                "camera_target": 1,
                "notes": mark.get("notes", ""),
            }
        )

    return found, ata_alignment_marks


def read_gds_library(gds_path: str) -> Any:
    gdstk = _require_gdstk()
    return gdstk.read_gds(gds_path)


def get_top_cell_names(lib: Any) -> List[str]:
    try:
        top_cells = lib.top_level()
    except Exception:
        top_cells = []
    names = [cell.name for cell in top_cells]
    if not names:
        names = [cell.name for cell in getattr(lib, "cells", [])]
    return sorted(names)


def copy_flattened_cell(cell: Any) -> Any:
    copy_name = f"__ATA_FLAT__{getattr(cell, 'name', 'CELL')}"
    try:
        copied = cell.copy(copy_name, deep_copy=True)
    except TypeError:
        try:
            copied = cell.copy(copy_name)
        except Exception:
            copied = cell
    try:
        copied.flatten(apply_repetitions=True)
    except TypeError:
        try:
            copied.flatten()
        except Exception:
            pass
    except Exception:
        pass
    return copied


def iter_cell_polygons(cell: Any) -> Iterable[Any]:
    return _sequence_or_empty(getattr(cell, "polygons", None))


def iter_cell_labels(cell: Any) -> Iterable[Any]:
    return _sequence_or_empty(getattr(cell, "labels", None))


def iter_cell_references(cell: Any) -> Iterable[Any]:
    return _sequence_or_empty(getattr(cell, "references", None))


def polygon_record(cell_name: str, index: int, polygon: Any) -> Dict[str, Any]:
    bbox = _bbox_from_object(polygon)
    rec: Dict[str, Any] = {
        "cell": cell_name,
        "polygon_index": index,
        "layer": getattr(polygon, "layer", ""),
        "datatype": getattr(polygon, "datatype", ""),
        "point_count": _safe_len(getattr(polygon, "points", None)),
    }
    rec.update(_bbox_to_record("bbox", bbox))
    return rec


def label_record(cell_name: str, index: int, label: Any) -> Dict[str, Any]:
    origin = _point_tuple(getattr(label, "origin", None))
    return {
        "cell": cell_name,
        "label_index": index,
        "text": getattr(label, "text", ""),
        "x_um": origin[0],
        "y_um": origin[1],
        "layer": getattr(label, "layer", ""),
        "texttype": getattr(label, "texttype", getattr(label, "datatype", "")),
        "anchor": getattr(label, "anchor", ""),
        "rotation": getattr(label, "rotation", ""),
        "magnification": getattr(label, "magnification", ""),
        "x_reflection": getattr(label, "x_reflection", ""),
    }


def reference_record(cell_name: str, index: int, ref: Any) -> Dict[str, Any]:
    origin = _point_tuple(getattr(ref, "origin", None))
    ref_cell_name = ""
    try:
        ref_cell_name = ref.cell.name
    except Exception:
        ref_cell_name = str(getattr(ref, "cell", getattr(ref, "cell_name", "")))

    spacing = getattr(ref, "spacing", None)
    spacing_x, spacing_y = _point_tuple(spacing) if spacing is not None else ("", "")

    bbox = _bbox_from_object(ref)
    rec: Dict[str, Any] = {
        "parent_cell": cell_name,
        "reference_index": index,
        "referenced_cell": ref_cell_name,
        "origin_x_um": origin[0],
        "origin_y_um": origin[1],
        "rotation": getattr(ref, "rotation", ""),
        "magnification": getattr(ref, "magnification", ""),
        "x_reflection": getattr(ref, "x_reflection", ""),
        "columns": getattr(ref, "columns", ""),
        "rows": getattr(ref, "rows", ""),
        "spacing_x_um": spacing_x,
        "spacing_y_um": spacing_y,
    }
    rec.update(_bbox_to_record("bbox", bbox))
    return rec


def collect_records(
    lib: Any,
    selected_cell_name: Optional[str] = None,
    pad_layer: Optional[int] = None,
    pad_datatype: Optional[int] = None,
    min_pad_size_um: float = 0.0,
    max_pad_size_um: float = 1e12,
    flatten_selected_for_pads: bool = True,
    alignment_mark_names: Optional[Any] = None,
    layout_metadata: Optional[Dict[str, Any]] = None,
    metadata_path: str = "",
) -> Dict[str, Any]:
    cells_by_name = _cell_dict(lib)
    top_cell_names = get_top_cell_names(lib)

    if selected_cell_name is None:
        selected_cell_name = top_cell_names[0] if top_cell_names else (next(iter(cells_by_name), ""))

    selected_cell = cells_by_name.get(selected_cell_name)
    if selected_cell is None and cells_by_name:
        selected_cell_name = next(iter(cells_by_name))
        selected_cell = cells_by_name[selected_cell_name]

    cells: List[Dict[str, Any]] = []
    polygons: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []
    layer_counter: Counter = Counter()

    for cell in _sequence_or_empty(getattr(lib, "cells", None)):
        bbox = _bbox_from_object(cell)
        cell_rec: Dict[str, Any] = {
            "cell": cell.name,
            "is_top_level": cell.name in top_cell_names,
            "polygon_count": len(iter_cell_polygons(cell)),
            "label_count": len(iter_cell_labels(cell)),
            "reference_count": len(iter_cell_references(cell)),
        }
        cell_rec.update(_bbox_to_record("bbox", bbox))
        cells.append(cell_rec)

        for i, poly in enumerate(iter_cell_polygons(cell), start=1):
            layer_key = (getattr(poly, "layer", ""), getattr(poly, "datatype", ""))
            layer_counter[layer_key] += 1
            polygons.append(polygon_record(cell.name, i, poly))

        for i, label in enumerate(iter_cell_labels(cell), start=1):
            layer_key = (getattr(label, "layer", ""), getattr(label, "texttype", getattr(label, "datatype", "")))
            layer_counter[layer_key] += 1
            labels.append(label_record(cell.name, i, label))

        for i, ref in enumerate(iter_cell_references(cell), start=1):
            references.append(reference_record(cell.name, i, ref))

    layers: List[Dict[str, Any]] = []
    for (layer, datatype), count in sorted(layer_counter.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
        poly_count = sum(1 for p in polygons if p.get("layer") == layer and p.get("datatype") == datatype)
        label_count = sum(1 for t in labels if t.get("layer") == layer and t.get("texttype") == datatype)
        layers.append(
            {
                "layer": layer,
                "datatype_or_texttype": datatype,
                "total_objects": count,
                "polygon_count": poly_count,
                "label_count": label_count,
            }
        )

    layout_metadata = layout_metadata or {}
    if pad_layer is None:
        meta_pad_layer = _metadata_value(layout_metadata, "pad_layer", "probe_pad_layer")
        if meta_pad_layer is not None:
            try:
                pad_layer = int(meta_pad_layer)
            except Exception:
                pass
    if pad_datatype is None:
        meta_pad_datatype = _metadata_value(layout_metadata, "pad_datatype", "probe_pad_datatype")
        if meta_pad_datatype is not None:
            try:
                pad_datatype = int(meta_pad_datatype)
            except Exception:
                pass

    pads, ata_pad_layout = extract_pads(
        selected_cell=selected_cell,
        selected_cell_name=selected_cell_name,
        pad_layer=pad_layer,
        pad_datatype=pad_datatype,
        min_pad_size_um=min_pad_size_um,
        max_pad_size_um=max_pad_size_um,
        flatten_selected=flatten_selected_for_pads,
    )

    alignment_marks, ata_alignment_marks = extract_alignment_marks(
        selected_cell=selected_cell,
        selected_cell_name=selected_cell_name,
        alignment_mark_names=alignment_mark_names,
    )

    ata_convention = extract_ata_convention_records(
        selected_cell=selected_cell,
        selected_cell_name=selected_cell_name,
    )

    existing_align_keys = {
        (str(r.get("mark_name", "")).upper(), round(_as_float(r.get("x_um")), 6), round(_as_float(r.get("y_um")), 6))
        for r in alignment_marks
    }
    for r in ata_convention.get("ata_metadata", []):
        if r.get("record_class") != "alignment_mark":
            continue
        key = (str(r.get("name", "")).upper(), round(_as_float(r.get("x_um")), 6), round(_as_float(r.get("y_um")), 6))
        if key in existing_align_keys:
            continue
        mark_id = f"ALIGN{len(alignment_marks) + 1:03d}"
        rec = {
            "mark_id": mark_id,
            "mark_name": r.get("name", ""),
            "matched_search_name": "ATA_ALIGN_*",
            "parent_cell": r.get("parent_cell", ""),
            "reference_index": "",
            "occurrence_path": r.get("occurrence_path", ""),
            "x_um": r.get("x_um", ""),
            "y_um": r.get("y_um", ""),
            "source": r.get("source", ""),
            "rotation": r.get("rotation", ""),
            "magnification": r.get("magnification", ""),
            "x_reflection": r.get("x_reflection", ""),
            "bbox_min_x_um": r.get("bbox_min_x_um", ""),
            "bbox_min_y_um": r.get("bbox_min_y_um", ""),
            "bbox_max_x_um": r.get("bbox_max_x_um", ""),
            "bbox_max_y_um": r.get("bbox_max_y_um", ""),
            "bbox_width_um": r.get("bbox_width_um", ""),
            "bbox_height_um": r.get("bbox_height_um", ""),
            "bbox_center_x_um": r.get("x_um", ""),
            "bbox_center_y_um": r.get("y_um", ""),
            "notes": "Auto-detected from ATA_ALIGN_* convention",
        }
        alignment_marks.append(rec)
        ata_alignment_marks.append(
            {
                "mark_id": mark_id,
                "mark_name": rec.get("mark_name", ""),
                "x_um": rec.get("x_um", ""),
                "y_um": rec.get("y_um", ""),
                "source": rec.get("source", ""),
                "gds_parent_cell": rec.get("parent_cell", ""),
                "occurrence_path": rec.get("occurrence_path", ""),
                "alignment_role": "",
                "camera_target": 1,
                "notes": rec.get("notes", ""),
            }
        )

    selected_bbox = _bbox_from_object(selected_cell) if selected_cell is not None else None
    summary = {
        "source_file": "",
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
        "library_name": getattr(lib, "name", ""),
        "unit": getattr(lib, "unit", ""),
        "precision": getattr(lib, "precision", ""),
        "cell_count": len(cells),
        "top_cells": top_cell_names,
        "selected_cell": selected_cell_name,
        "selected_cell_bbox": selected_bbox,
        "polygon_count": len(polygons),
        "label_count": len(labels),
        "reference_count": len(references),
        "layer_count": len(layers),
        "pad_count": len(pads),
        "alignment_mark_count": len(alignment_marks),
        "alignment_mark_search_names": parse_name_list(alignment_mark_names) or parse_name_list(default_alignment_mark_names()),
        "ata_metadata_count": len(ata_convention.get("ata_metadata", [])),
        "ata_device_count": len(ata_convention.get("ata_devices", [])),
        "ata_test_structure_count": len(ata_convention.get("ata_test_structures", [])),
        "layout_metadata_loaded": bool(layout_metadata),
        "layout_metadata_path": metadata_path,
        "pad_layer": pad_layer,
        "pad_datatype": pad_datatype,
        "flatten_selected_for_pads": flatten_selected_for_pads,
    }

    result = {
        "summary": summary,
        "top_cell_names": top_cell_names,
        "selected_cell_name": selected_cell_name,
        "cells": cells,
        "layers": layers,
        "polygons": polygons,
        "labels": labels,
        "references": references,
        "pads": pads,
        "ata_pad_layout": ata_pad_layout,
        "alignment_marks": alignment_marks,
        "ata_alignment_marks": ata_alignment_marks,
        "ata_metadata": ata_convention.get("ata_metadata", []),
        "ata_die_markers": ata_convention.get("ata_die_markers", []),
        "ata_devices": ata_convention.get("ata_devices", []),
        "ata_test_structures": ata_convention.get("ata_test_structures", []),
        "ata_sites": ata_convention.get("ata_sites", []),
        "ata_channel_map": ata_convention.get("ata_channel_map", []),
        "layout_metadata": metadata_to_records(layout_metadata, metadata_path),
        "layout_metadata_raw": layout_metadata,
        "layout_metadata_path": metadata_path,
    }
    result["ata_validation_report"] = validate_ata_convention(result, layout_metadata, metadata_path)
    return result


def extract_pads(
    selected_cell: Any,
    selected_cell_name: str,
    pad_layer: Optional[int],
    pad_datatype: Optional[int],
    min_pad_size_um: float,
    max_pad_size_um: float,
    flatten_selected: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if selected_cell is None or pad_layer is None:
        return [], []

    source_cell = copy_flattened_cell(selected_cell) if flatten_selected else selected_cell
    source_cell_name = selected_cell_name + ("__flattened" if flatten_selected else "")

    label_candidates = []
    for label in iter_cell_labels(source_cell):
        rec = label_record(source_cell_name, len(label_candidates) + 1, label)
        if str(rec.get("text", "")).strip():
            label_candidates.append(rec)

    pads: List[Dict[str, Any]] = []
    for i, poly in enumerate(iter_cell_polygons(source_cell), start=1):
        layer = getattr(poly, "layer", None)
        datatype = getattr(poly, "datatype", None)
        if layer != pad_layer:
            continue
        if pad_datatype is not None and datatype != pad_datatype:
            continue
        bbox = _bbox_from_object(poly)
        if bbox is None:
            continue
        width, height = _bbox_size(bbox)
        largest = max(abs(width), abs(height))
        smallest = min(abs(width), abs(height))
        if largest < min_pad_size_um:
            continue
        if smallest <= 0:
            continue
        if largest > max_pad_size_um:
            continue

        center_x, center_y = _bbox_center(bbox)
        label_text = _match_label_to_bbox(label_candidates, bbox)
        pad_id = f"PAD{len(pads) + 1:03d}"
        pad_name = label_text or pad_id
        rec: Dict[str, Any] = {
            "pad_id": pad_id,
            "pad_name": pad_name,
            "source_cell": selected_cell_name,
            "source_polygon_index": i,
            "layer": layer,
            "datatype": datatype,
            "x_um": center_x,
            "y_um": center_y,
            "width_um": width,
            "height_um": height,
            "area_bbox_um2": width * height,
            "label_text": label_text,
        }
        rec.update(_bbox_to_record("bbox", bbox))
        pads.append(rec)

    ata_pad_layout: List[Dict[str, Any]] = []
    for pad in pads:
        ata_pad_layout.append(
            {
                "pad_id": pad["pad_id"],
                "pad_name": pad["pad_name"],
                "x_um": pad["x_um"],
                "y_um": pad["y_um"],
                "width_um": pad["width_um"],
                "height_um": pad["height_um"],
                "layer": pad["layer"],
                "datatype": pad["datatype"],
                "instrument": "",
                "channel": "",
                "net_name": pad.get("label_text", ""),
                "notes": "",
            }
        )

    return pads, ata_pad_layout


def _match_label_to_bbox(labels: Sequence[Dict[str, Any]], bbox: BBox) -> str:
    if not labels:
        return ""

    inside = []
    for label in labels:
        x = _as_float(label.get("x_um"))
        y = _as_float(label.get("y_um"))
        if _inside_bbox(x, y, bbox):
            inside.append(label)
    if inside:
        return str(inside[0].get("text", "")).strip()

    cx, cy = _bbox_center(bbox)
    width, height = _bbox_size(bbox)
    max_dist = max(50.0, 2.0 * max(abs(width), abs(height)))
    best_label = None
    best_dist = None
    for label in labels:
        x = _as_float(label.get("x_um"))
        y = _as_float(label.get("y_um"))
        dist = math.hypot(x - cx, y - cy)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_label = label
    if best_label is not None and best_dist is not None and best_dist <= max_dist:
        return str(best_label.get("text", "")).strip()
    return ""


def generate_wafer_map(
    selected_cell_bbox: Optional[BBox],
    selected_cell_references: Sequence[Dict[str, Any]],
    wafer_diameter_mm: float = 200.0,
    edge_exclusion_mm: float = 3.0,
    die_pitch_x_um: Optional[float] = None,
    die_pitch_y_um: Optional[float] = None,
    use_references_if_available: bool = True,
) -> List[Dict[str, Any]]:
    if use_references_if_available and len(selected_cell_references) > 1:
        rows = []
        for i, ref in enumerate(selected_cell_references, start=1):
            rows.append(
                {
                    "die_id": f"D{i:05d}",
                    "source": "gds_references",
                    "row": "",
                    "col": "",
                    "x_um": ref.get("origin_x_um", ""),
                    "y_um": ref.get("origin_y_um", ""),
                    "cell_name": ref.get("referenced_cell", ""),
                    "site": "",
                    "test_enabled": 1,
                    "bin": "UNTESTED",
                    "notes": "Generated from selected top-cell references",
                }
            )
        return rows

    if die_pitch_x_um is None or die_pitch_x_um <= 0:
        die_pitch_x_um = _bbox_size(selected_cell_bbox)[0] if selected_cell_bbox else 1000.0
    if die_pitch_y_um is None or die_pitch_y_um <= 0:
        die_pitch_y_um = _bbox_size(selected_cell_bbox)[1] if selected_cell_bbox else 1000.0
    if die_pitch_x_um <= 0:
        die_pitch_x_um = 1000.0
    if die_pitch_y_um <= 0:
        die_pitch_y_um = 1000.0

    wafer_radius_um = (wafer_diameter_mm * 1000.0) / 2.0
    usable_radius_um = max(0.0, wafer_radius_um - edge_exclusion_mm * 1000.0)
    max_col = int(math.floor(usable_radius_um / die_pitch_x_um))
    max_row = int(math.floor(usable_radius_um / die_pitch_y_um))

    rows: List[Dict[str, Any]] = []
    for row in range(-max_row, max_row + 1):
        for col in range(-max_col, max_col + 1):
            x = col * die_pitch_x_um
            y = row * die_pitch_y_um
            if math.hypot(x, y) <= usable_radius_um:
                rows.append(
                    {
                        "die_id": f"X{col:+04d}_Y{row:+04d}",
                        "source": "generated_grid",
                        "row": row,
                        "col": col,
                        "x_um": x,
                        "y_um": y,
                        "cell_name": "",
                        "site": "",
                        "test_enabled": 1,
                        "bin": "UNTESTED",
                        "notes": "Generated circular wafer grid",
                    }
                )
    return rows


def selected_cell_references(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    selected = data.get("selected_cell_name", "")
    return [r for r in data.get("references", []) if r.get("parent_cell") == selected]


def default_die_pitch_from_summary(data: Dict[str, Any]) -> Tuple[float, float]:
    bbox = data.get("summary", {}).get("selected_cell_bbox")
    if bbox is None:
        return (1000.0, 1000.0)
    try:
        return _bbox_size(tuple(bbox))
    except Exception:
        return (1000.0, 1000.0)


def write_csv(path: str, records: Sequence[Dict[str, Any]], default_headers: Optional[Sequence[str]] = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    headers: List[str] = []
    if default_headers:
        headers.extend(default_headers)
    for rec in records:
        for key in rec.keys():
            if key not in headers:
                headers.append(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers or ["empty"])
        writer.writeheader()
        for rec in records:
            writer.writerow({h: _json_safe(rec.get(h, "")) for h in headers})


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        if hasattr(value, "tolist"):
            return value.tolist()
    except Exception:
        pass
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


GDS_SUBFOLDER = "gds"


def export_ata_files(
    data: Dict[str, Any],
    output_dir: str,
    wafer_map: Optional[Sequence[Dict[str, Any]]] = None,
    source_file: str = "",
) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    gds_dir = os.path.join(output_dir, GDS_SUBFOLDER)
    os.makedirs(gds_dir, exist_ok=True)

    summary = dict(data.get("summary", {}))
    summary["source_file"] = source_file
    summary["exported_at"] = datetime.now().isoformat(timespec="seconds")
    summary["output_dir"] = output_dir
    summary["wafer_map_count"] = len(wafer_map) if wafer_map is not None else 0

    files: Dict[str, str] = {}
    files["run_summary"] = os.path.join(gds_dir, "run_summary.json")
    with open(files["run_summary"], "w", encoding="utf-8") as f:
        json.dump(_json_safe(summary), f, indent=2)

    if data.get("layout_metadata_raw"):
        files["layout_metadata_json"] = os.path.join(gds_dir, "layout_metadata.json")
        with open(files["layout_metadata_json"], "w", encoding="utf-8") as f:
            json.dump(_json_safe(data.get("layout_metadata_raw", {})), f, indent=2)

    root_export_map = {
        "ata_pad_layout": "ata_pad_layout.csv",
        "alignment_marks": "alignment_marks.csv",
        "ata_alignment_marks": "ata_alignment_marks.csv",
        "ata_metadata": "ata_metadata.csv",
    }
    for key, filename in root_export_map.items():
        files[key] = os.path.join(output_dir, filename)
        write_csv(files[key], data.get(key, []))

    gds_export_map = {
        "cells": "cells.csv",
        "layers": "layers.csv",
        "polygons": "polygons.csv",
        "labels": "labels.csv",
        "references": "references.csv",
        "pads": "pads.csv",
        "ata_die_markers": "ata_die_markers.csv",
        "ata_devices": "ata_devices.csv",
        "ata_test_structures": "ata_test_structures.csv",
        "ata_sites": "ata_sites.csv",
        "ata_channel_map": "ata_channel_map.csv",
        "layout_metadata": "layout_metadata.csv",
        "ata_validation_report": "ata_validation_report.csv",
    }
    for key, filename in gds_export_map.items():
        files[key] = os.path.join(gds_dir, filename)
        write_csv(files[key], data.get(key, []))

    if wafer_map is not None:
        files["ata_wafer_map"] = os.path.join(output_dir, "ata_wafer_map_gds.csv")
        write_csv(
            files["ata_wafer_map"],
            wafer_map,
            default_headers=["die_id", "source", "row", "col", "x_um", "y_um", "cell_name", "site", "test_enabled", "bin", "notes"],
        )

    test_plan = build_first_pass_test_plan(data, wafer_map if wafer_map is not None else [], source_file=source_file)
    files["ata_test_plan"] = os.path.join(gds_dir, "ata_test_plan.json")
    with open(files["ata_test_plan"], "w", encoding="utf-8") as f:
        json.dump(_json_safe(test_plan), f, indent=2)

    return files


def build_first_pass_test_plan(data: Dict[str, Any], wafer_map: Sequence[Dict[str, Any]], source_file: str = "") -> Dict[str, Any]:
    summary = data.get("summary", {})
    return {
        "ata_version": "phase_1_gds_import",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_gds_file": source_file,
        "selected_cell": data.get("selected_cell_name", ""),
        "layout_unit_note": "Coordinates are exported in GDS user units. Most MEMS/IC layouts use microns; verify against the source design.",
        "summary": {
            "cell_count": summary.get("cell_count", 0),
            "layer_count": summary.get("layer_count", 0),
            "pad_count": summary.get("pad_count", 0),
            "alignment_mark_count": summary.get("alignment_mark_count", 0),
            "ata_metadata_count": summary.get("ata_metadata_count", 0),
            "ata_device_count": summary.get("ata_device_count", 0),
            "ata_test_structure_count": summary.get("ata_test_structure_count", 0),
            "wafer_map_count": len(wafer_map),
        },
        "pad_layout_file": "ata_pad_layout.csv",
        "alignment_marks_file": "ata_alignment_marks.csv",
        "ata_metadata_file": "ata_metadata.csv",
        "ata_validation_report_file": "ata_validation_report.csv",
        "wafer_map_file": "ata_wafer_map_gds.csv",
        "default_recipe": {
            "recipe_name": "UNASSIGNED_PHASE_1_PLACEHOLDER",
            "description": "Fill this in with the actual ATA measurement recipe after pad/instrument mapping is complete.",
            "tests": [],
        },
        "instrument_channel_mapping_required": True,
    }
