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
