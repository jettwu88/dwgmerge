"""
dwgmerge_engine.core
=====================
Reusable engine that merges a "B" engineering drawing (its model-space
geometry + paperspace annotations) into the title-block / frame standard
defined by an "A" template drawing.

This generalises the manual workflow validated on:
  A = Wellell-standard-V7-2004.dxf
  B = 871203-0000_V1.0.dxf

Everything here operates on DXF (via ezdxf). DWG is out of scope for this
module - see dwgmerge_app/convert.py for the (manual, non-automated)
DWG<->DXF story.
"""
from __future__ import annotations

import copy
import dataclasses
import re
from typing import Optional

import ezdxf
from ezdxf.addons import importer
from ezdxf.math import Vec3
from ezdxf import bbox as ezbbox

# ---------------------------------------------------------------------
# "Clean" engineering scales we are allowed to land on. We never emit a
# scale like 1:3.34 or 1:511 - only one of these (or its N:1 enlargement
# mirror) is used, per the "要讓模型整數比例放入圖框" requirement.
# ---------------------------------------------------------------------
CLEAN_SCALE_STEPS = [1, 1.5, 2, 2.5, 3, 4, 5, 10, 15, 20, 25, 50, 100, 200, 500, 1000]


@dataclasses.dataclass
class ScaleResult:
    ratio_text: str      # e.g. "1:4" or "2:1"
    factor: float         # paper_units = factor * model_units


def compute_clean_scale(model_w: float, model_h: float,
                         avail_w: float, avail_h: float) -> ScaleResult:
    """Pick the largest *reduction* scale (1:N, smallest N) that fits the
    model inside (avail_w, avail_h). If the model is small enough that
    1:1 leaves most of the area empty, try an *enlargement* scale (N:1)
    instead so the drawing isn't left tiny in a huge sheet.
    """
    if model_w <= 0 or model_h <= 0:
        return ScaleResult("1:1", 1.0)

    # --- reduction candidates: paper = model / N ---
    best_reduction = None
    for n in CLEAN_SCALE_STEPS:
        factor = 1.0 / n
        if model_w * factor <= avail_w and model_h * factor <= avail_h:
            best_reduction = (n, factor)
            break  # CLEAN_SCALE_STEPS is ascending -> first hit is the largest scale that fits

    if best_reduction is not None:
        n, factor = best_reduction
        if n == 1:
            # Check whether we are leaving so much of the sheet empty that
            # an enlargement would represent the part better.
            area_ratio = (model_w * model_h) / (avail_w * avail_h)
            if area_ratio < 0.15:
                for n2 in CLEAN_SCALE_STEPS:
                    if n2 == 1:
                        continue
                    factor2 = float(n2)
                    if model_w * factor2 <= avail_w and model_h * factor2 <= avail_h:
                        best_enlarge = (n2, factor2)
                    else:
                        break
                else:
                    best_enlarge = None
                # walk again correctly (need the LARGEST n2 that still fits)
                best_enlarge = None
                for n2 in CLEAN_SCALE_STEPS:
                    if n2 == 1:
                        continue
                    factor2 = float(n2)
                    if model_w * factor2 <= avail_w and model_h * factor2 <= avail_h:
                        best_enlarge = (n2, factor2)
                    else:
                        break
                if best_enlarge is not None:
                    n2, factor2 = best_enlarge
                    return ScaleResult(f"{n2}:1", factor2)
        return ScaleResult(f"1:{n}", factor)

    # Nothing in our reduction table fits (a very large part) - fall back
    # to the smallest reduction available (biggest N) even if it clips;
    # caller should flag this to the user.
    n = CLEAN_SCALE_STEPS[-1]
    return ScaleResult(f"1:{n}", 1.0 / n)


# ---------------------------------------------------------------------
# Title block discovery
# ---------------------------------------------------------------------

def find_titleblock_insert(layout):
    """Heuristic: the title block is the INSERT entity in this layout
    carrying the most ATTRIB entities."""
    best = None
    best_count = -1
    for e in layout:
        if e.dxftype() == "INSERT":
            n = len(list(e.attribs))
            if n > best_count:
                best = e
                best_count = n
    return best


def attdefs_of(block):
    return [e for e in block if e.dxftype() == "ATTDEF"]


def layout_paper_size(layout):
    return layout.dxf.paper_width, layout.dxf.paper_height


def list_layout_names(doc):
    return [l.name for l in doc.layouts if l.name.lower() != "model"]


# ---------------------------------------------------------------------
# Attribute carry-over: map OLD title block's attrib values onto the NEW
# title block's ATTDEFs. Tries (tag, local-y) first (works when A and B
# share the same template lineage, as validated on the Wellell family),
# then falls back to plain tag matching (works for simpler / unrelated
# templates where positions won't line up).
# ---------------------------------------------------------------------

def carry_over_attributes(old_insert, new_insert_attdefs, old_insert_pos):
    by_tag_pos = {}
    by_tag = {}
    for a in old_insert.attribs:
        local_y = round(a.dxf.insert.y - old_insert_pos.y, 1)
        by_tag_pos.setdefault((a.dxf.tag, local_y), a.dxf.text)
        by_tag.setdefault(a.dxf.tag, []).append(a.dxf.text)

    used_tag_counts = {}
    result = []
    for ad in new_insert_attdefs:
        key = (ad.dxf.tag, round(ad.dxf.insert.y, 1))
        if key in by_tag_pos:
            value = by_tag_pos[key]
        elif ad.dxf.tag in by_tag:
            idx = used_tag_counts.get(ad.dxf.tag, 0)
            values = by_tag[ad.dxf.tag]
            value = values[idx] if idx < len(values) else values[-1]
            used_tag_counts[ad.dxf.tag] = idx + 1
        else:
            value = ad.dxf.text  # template default
        result.append((ad, value))
    return result


def place_titleblock(dst_doc, dst_layout, block_name, insert_pos, xscale, yscale,
                      rotation, attdef_value_pairs):
    new_insert = dst_layout.add_blockref(
        block_name, insert_pos,
        dxfattribs={"xscale": xscale, "yscale": yscale, "rotation": rotation},
    )
    for ad, value in attdef_value_pairs:
        world_pos = Vec3(ad.dxf.insert.x + insert_pos.x,
                          ad.dxf.insert.y + insert_pos.y,
                          ad.dxf.insert.z)
        new_insert.add_attrib(ad.dxf.tag, value, world_pos, dxfattribs={
            "height": ad.dxf.height,
            "style": ad.dxf.style,
            "layer": ad.dxf.layer,
        })
    return new_insert


def scale_attrib_text(tag: str, value: str, scale: ScaleResult) -> str:
    """Special-case the 比例/Scale field: replace whatever was there with
    the computed clean ratio, unless the drawing is a semi-finished
    assembly (caller decides that and skips calling this)."""
    return scale.ratio_text


# ---------------------------------------------------------------------
# Model bbox / viewport helpers
# ---------------------------------------------------------------------

def model_bbox(doc):
    msp = doc.modelspace()
    box = ezbbox.extents(msp, fast=False)
    return box


def find_content_viewport(layout):
    """The real content viewport is the one that is NOT the paperspace
    overview (that one always has dxf.status/id == 1)."""
    candidates = [e for e in layout if e.dxftype() == "VIEWPORT" and e.dxf.status != 1]
    if not candidates:
        return None
    # pick the largest one by area - most likely the main content view
    candidates.sort(key=lambda e: e.dxf.width * e.dxf.height, reverse=True)
    return candidates[0]


def apply_scale_to_viewport(vp, scale: ScaleResult, center: Vec3):
    vp.dxf.view_height = vp.dxf.height / scale.factor
    vp.dxf.view_center_point = Vec3(center.x, center.y, 0.0)


# ---------------------------------------------------------------------
# Layer / style harmonisation (used by mode 1: "B content, A everything
# else")
# ---------------------------------------------------------------------

def harmonize_layers(src_doc, dst_doc, only_names: Optional[set] = None):
    """Copy color / linetype / lineweight from src_doc's layer table onto
    matching-name layers in dst_doc (A's standard wins). Layers that only
    exist in dst_doc (B-specific, e.g. a legacy legend-table layer with no
    A equivalent) are left untouched."""
    changed = []
    for layer in dst_doc.layers:
        if only_names is not None and layer.dxf.name not in only_names:
            continue
        src_layer = src_doc.layers.get(layer.dxf.name) if layer.dxf.name in {l.dxf.name for l in src_doc.layers} else None
        if src_layer is None:
            continue
        before = (layer.dxf.color, layer.dxf.linetype, layer.dxf.lineweight)
        layer.dxf.color = src_layer.dxf.color
        layer.dxf.linetype = src_layer.dxf.linetype if src_layer.dxf.linetype in dst_doc.linetypes else layer.dxf.linetype
        layer.dxf.lineweight = src_layer.dxf.lineweight
        after = (layer.dxf.color, layer.dxf.linetype, layer.dxf.lineweight)
        if before != after:
            changed.append(layer.dxf.name)
    return changed


def harmonize_text_styles(src_doc, dst_doc, style_names):
    changed = []
    for name in style_names:
        if name in {s.dxf.name for s in dst_doc.styles} and name in {s.dxf.name for s in src_doc.styles}:
            dst_style = dst_doc.styles.get(name)
            src_style = src_doc.styles.get(name)
            dst_style.dxf.font = src_style.dxf.font
            dst_style.dxf.bigfont = src_style.dxf.bigfont
            changed.append(name)
    return changed


def set_lineweight_for_undefined_layers(doc, lineweight_1_100mm: int, skip_names: set):
    """Set an explicit lineweight on any layer that currently has no
    explicit value (-3 = LAYER DEFAULT) and isn't one of the layers the
    template (A) already defines explicitly (those are left alone)."""
    changed = []
    for layer in doc.layers:
        if layer.dxf.name in skip_names:
            continue
        if layer.dxf.lineweight == -3:
            layer.dxf.lineweight = lineweight_1_100mm
            changed.append(layer.dxf.name)
    return changed


# ---------------------------------------------------------------------
# Auto-detection helpers (roadmap items #1 and #2: auto paper-size
# selection, auto semi-finished detection) - both are advisory, always
# meant to be overridable by hand in the UI, never a silent decision.
# ---------------------------------------------------------------------

AUTO_SHEET_MARGIN_MM = 20.0  # matches the printable-area margin used
                             # elsewhere for clean-scale fitting


def _layout_area(layout) -> float:
    return layout.dxf.paper_width * layout.dxf.paper_height


def suggest_target_layout(a_doc, b_doc) -> tuple[str, str]:
    """Auto-pick which of A's layouts best fits B's model, based on raw
    (unscaled) model size vs. each layout's printable area - NOT on the
    scale that would eventually be used (compute_clean_scale can always
    shrink a model to fit, that's a separate step; this is purely about
    "is this basically a small part or a big one").

    Rule (as specified by the user): if B's model fits inside the
    smallest available sheet (e.g. A4) without needing much reduction,
    use that one - and prefer its portrait/vertical variant (a layout
    name ending in "-V") when the model itself is taller than it is
    wide; otherwise fall back to the largest available sheet (e.g. A3).
    Only two tiers are considered because that's what the current
    template family provides (A4 / A4-V / A3) - if a future template
    adds more sizes, this still degrades sanely: smallest-that-fits,
    else largest overall.

    Returns (layout_name, note) - `note` is a short, user-facing
    Traditional Chinese explanation of why this layout was picked (or
    why the auto-pick gave up and fell back), meant to be shown next to
    the auto-selected value so the user can sanity-check or override
    it by hand.
    """
    names = list_layout_names(a_doc)
    if not names:
        raise ValueError("A 模板沒有任何圖紙 Layout，無法自動選擇圖紙尺寸。")

    by_area = sorted(names, key=lambda n: _layout_area(a_doc.layout(n)))
    smallest_name = by_area[0]
    largest_name = by_area[-1]

    box = model_bbox(b_doc)
    if not box.has_data:
        return largest_name, (
            f"無法量測 B 圖模型範圍的大小，已自動選擇最大的圖紙尺寸「{largest_name}」，請自行確認。"
        )
    mw, mh = box.size.x, box.size.y

    def fits(name: str) -> bool:
        lay = a_doc.layout(name)
        pw = lay.dxf.paper_width - AUTO_SHEET_MARGIN_MM
        ph = lay.dxf.paper_height - AUTO_SHEET_MARGIN_MM
        return mw <= pw and mh <= ph

    def vertical_variant_of(name: str) -> Optional[str]:
        base = name.upper().replace(" ", "").replace("-V", "")
        for n in names:
            if n.upper().replace(" ", "") in (f"{base}-V", f"{base}V"):
                return n
        return None

    if fits(smallest_name):
        portrait = mh > mw
        v_name = vertical_variant_of(smallest_name)
        if portrait and v_name:
            return v_name, (
                f"模型尺寸約 {mw:.0f}x{mh:.0f}mm，偏直式，且在「{smallest_name}」的可用範圍內，"
                f"自動選擇直式圖紙「{v_name}」。"
            )
        return smallest_name, (
            f"模型尺寸約 {mw:.0f}x{mh:.0f}mm，在最小圖紙「{smallest_name}」的可用範圍內，"
            f"自動選擇「{smallest_name}」。"
        )

    if largest_name != smallest_name:
        return largest_name, (
            f"模型尺寸約 {mw:.0f}x{mh:.0f}mm，超出最小圖紙「{smallest_name}」的可用範圍，"
            f"自動選擇最大的圖紙「{largest_name}」（仍會依比例縮小到能放入圖框）。"
        )
    return largest_name, f"模型尺寸約 {mw:.0f}x{mh:.0f}mm，自動選擇「{largest_name}」。"


# ---------------------------------------------------------------------
# Output filename: 料號 + 版次 (auto-incremented) + 日期流水號, e.g.
# "871203-0000 V1.1_091701" for a merge of "871203-0000_V1.0.dxf" done
# on 09/17, first file in that batch.
# ---------------------------------------------------------------------

_VERSION_RE = re.compile(r"^[Vv]\s*(\d+)(?:\.(\d+))?$")
_FILENAME_PART_VERSION_RE = re.compile(
    r"^(?P<part>.+?)[\s_]+[Vv](?P<major>\d+)(?:\.(?P<minor>\d+))?\s*$"
)


def parse_version_string(text: Optional[str]) -> Optional[tuple[int, int]]:
    """"V1.0" / "v2" / "V3.14" -> (major, minor). None if `text` doesn't
    look like a version marker at all (empty, or some other value the
    title block's 版次 field happens to hold)."""
    if not text:
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    return major, minor


def format_version(version: tuple[int, int]) -> str:
    major, minor = version
    return f"V{major}.{minor}"


def next_version(version: tuple[int, int]) -> tuple[int, int]:
    major, minor = version
    return major, minor + 1


def parse_filename_part_and_version(stem: str) -> tuple[str, Optional[tuple[int, int]]]:
    """From a B filename's stem (no extension), split off a trailing
    "_V<major>.<minor>"-style version marker if there is one. Returns
    (part_no, version) - version is None (and the whole stem is
    returned as the part number) if no such marker is found."""
    m = _FILENAME_PART_VERSION_RE.match(stem)
    if not m:
        return stem, None
    major = int(m.group("major"))
    minor = int(m.group("minor")) if m.group("minor") else 0
    return m.group("part"), (major, minor)


def resolve_part_and_version(filename_stem: str, b_tb_attribs: dict) -> tuple[str, tuple[int, int], list[str]]:
    """Combine B's own filename with B's own title block fields (料號 =
    the 圖號 attrib, 版本 = the 版次 attrib) into one (part_no, version,
    notes) triple for naming the merged output.

    Rule (as specified): if the filename's version and the title
    block's version disagree, the title block (drawing content) wins;
    if only one side has a version at all, use whichever one does.
    Part number always prefers the title block's own 圖號 field (that's
    the authoritative value already carried into the merged drawing),
    falling back to whatever the filename parses out.
    """
    notes = []
    fn_part, fn_version = parse_filename_part_and_version(filename_stem)
    content_part = (b_tb_attribs.get("圖號") or "").strip() or None
    content_version = parse_version_string(b_tb_attribs.get("版次"))

    part_no = content_part or fn_part or filename_stem

    if content_version and fn_version and content_version != fn_version:
        notes.append(
            f"檔名的版本（{format_version(fn_version)}）跟圖檔標題欄的版次"
            f"（{format_version(content_version)}）不一致，命名以圖檔內容為準。"
        )
        version = content_version
    else:
        version = content_version or fn_version

    if version is None:
        notes.append("找不到版本資訊（檔名跟標題欄的版次欄位都沒有），輸出檔名已預設為 V1.0。")
        version = (1, 0)

    return part_no, version, notes


def b_titleblock_attribs(b_doc) -> dict:
    """B's own title block ATTRIB values as a {tag: text} dict (last
    value wins if a tag repeats, e.g. multiple 簽名日期 rows) - used to
    read 圖號/版次 for output-filename purposes without needing the full
    merge to have run yet."""
    names = list_layout_names(b_doc)
    if not names:
        return {}
    lay = b_doc.layout(names[0])
    tb = find_titleblock_insert(lay)
    if tb is None:
        return {}
    result = {}
    for a in tb.attribs:
        result[a.dxf.tag] = a.dxf.text
    return result


def build_output_stem(part_no: str, version: tuple[int, int], date_mmdd: str) -> str:
    """"871203-0000 V1.1_0917" - 料號 + 版次_日期。"""
    return f"{part_no} {format_version(version)}_{date_mmdd}"


# ---------------------------------------------------------------------
# Usage stats: turn the raw "timestamp,event,filename" CSV log (see
# github_store.append_usage_event / download_usage_log) into the
# numbers the "使用統計" tab shows - total downloads, this month, this
# year, and a per-month breakdown for a simple trend view.
# ---------------------------------------------------------------------

def compute_usage_stats(csv_bytes: Optional[bytes], now=None) -> dict:
    import csv
    import io as _io
    from datetime import datetime as _dt

    now = now or _dt.now()
    result = {"total": 0, "this_month": 0, "this_year": 0, "by_month": {}}
    if not csv_bytes:
        return result

    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.reader(_io.StringIO(text))
    rows = list(reader)
    if rows and rows[0] and rows[0][0] == "timestamp":
        rows = rows[1:]

    by_month: dict[str, int] = {}
    total = 0
    this_month = 0
    this_year = 0
    for row in rows:
        if not row or not row[0]:
            continue
        try:
            dt = _dt.fromisoformat(row[0])
        except ValueError:
            continue
        total += 1
        key = dt.strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0) + 1
        if dt.year == now.year:
            this_year += 1
            if dt.month == now.month:
                this_month += 1

    result["total"] = total
    result["this_month"] = this_month
    result["this_year"] = this_year
    result["by_month"] = dict(sorted(by_month.items()))
    return result


def b_has_bom_objects(b_doc) -> bool:
    """Whether B's own paperspace layout contains an embedded OLE object
    (OLE2FRAME - e.g. a pasted Excel BOM table), used to auto-detect
    "this is a semi-finished/assembly drawing" without requiring the
    user to check a box by hand."""
    names = list_layout_names(b_doc)
    if not names:
        return False
    b_layout = b_doc.layout(names[0])
    return any(e.dxftype() == "OLE2FRAME" for e in b_layout)
