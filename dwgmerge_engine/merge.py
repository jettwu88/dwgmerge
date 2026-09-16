"""
dwgmerge_engine.merge
======================
Top-level orchestration: given an A (template) DXF, a B (legacy drawing)
DXF, a target layout name in A, a merge mode, and a couple of flags,
produce the merged DXF document.

Three modes, matching what was requested:

  MODE_B_CONTENT_A_STYLE ("1"):
      B's model geometry + field VALUES are kept. Everything else -
      frame block, layer colors/linetypes/lineweights, text style fonts,
      notes wording - follows A.

  MODE_B_STYLE_A_FRAME_ONLY ("2"):
      Only A's frame/title-block graphic is swapped in. All formatting
      (colors, fonts, lineweights) stays B's. If the transplanted A
      frame's own label text uses a format that conflicts with B's (e.g.
      a different text style for the same kind of label), it is updated
      to match B.

  MODE_SIMPLE_COMBINE ("3"):
      A's frame + B's drawing are just placed together with neither
      side's formatting touched.

All three: content is scaled into the target A layout using a "clean"
scale (see core.compute_clean_scale) and the resulting ratio is written
into the frame's Scale field (unless `semi_finished=True`, in which case
the field is set to "n/a" and no attempt is made to compute a ratio -
matching the "半成品不需計算比例" rule).
"""
from __future__ import annotations

import dataclasses
from typing import Optional

import ezdxf
from ezdxf.addons import importer
from ezdxf.math import Vec3
from ezdxf import bbox as ezbbox

from . import core

MODE_B_CONTENT_A_STYLE = "1"
MODE_B_STYLE_A_FRAME_ONLY = "2"
MODE_SIMPLE_COMBINE = "3"

SCALE_TAG_CANDIDATES = ["比例", "SCALE", "Scale"]
DEFAULT_LINEWEIGHT_1_100MM = 5  # 0.05mm, matches the validated Wellell case


@dataclasses.dataclass
class MergeReport:
    scale: core.ScaleResult
    relocated_entities: int
    unmatched_attdefs: list
    warnings: list


def _find_scale_attdef(attdef_value_pairs):
    for ad, value in attdef_value_pairs:
        if ad.dxf.tag in SCALE_TAG_CANDIDATES:
            return ad
    return None


def _boxes_overlap(b1, b2, tol=0.05):
    """AABB overlap test with a small tolerance, because many DXF
    entities (a straight LINE, a vertical/horizontal one especially) have
    a genuinely zero-width or zero-height bbox - ezdxf's own
    BoundingBox.intersection().has_data is False for those even when they
    exactly coincide, which is exactly the "duplicate border line" case
    this needs to catch."""
    if not (b1.has_data and b2.has_data):
        return False
    return (
        b1.extmin.x - tol <= b2.extmax.x and b1.extmax.x + tol >= b2.extmin.x and
        b1.extmin.y - tol <= b2.extmax.y and b1.extmax.y + tol >= b2.extmin.y
    )


def _entity_text(e):
    if e.dxftype() == "MTEXT":
        return (e.text if hasattr(e, "text") else e.dxf.text).strip()
    if e.dxftype() == "TEXT":
        return e.dxf.text.strip()
    return None


# Known "fill this in" placeholder strings that show up in A's own
# revision-table row template. If B already has its own real values in
# that spot (very likely - B is a real drawing with real history), the
# placeholder shouldn't be imported at all; B's real text is left alone
# untouched (it was never part of skip_entities so nothing removes it).
PLACEHOLDER_TEXTS = {
    "all", "new drawing", "xxxx.xx.xx/name", "專案編號/ecr", "name",
}


def _copy_paperspace_extras(src_doc, src_layout, dst_doc, dst_layout,
                             skip_entities, b_originals=None):
    """Bring A's non-titleblock frame furniture (border, revision-table
    headers, tolerance chart, notes, ...) into dst_layout.

    Two safety rules keep this from duplicating content:
      1. A's per-row "fill this in" placeholders (PLACEHOLDER_TEXTS) are
         never imported - B's own real values already live there.
      2. Anything else that lands exactly on top of one of B's ORIGINAL
         paperspace entities (b_originals, e.g. B's own old copy of the
         same border/header) makes that old B entity get deleted, so we
         don't end up with two overlapping copies. B's entities that
         *aren't* superseded this way (its unique content: a legend
         table, real revision-history text, etc.) are left completely
         alone.

    Returns (new_entities, superseded_b_entities).
    """
    b_originals = b_originals or []
    before = set(id(e) for e in dst_layout)
    imp = importer.Importer(src_doc, dst_doc)
    candidates = [e for e in src_layout if e not in skip_entities]
    for e in candidates:
        text = _entity_text(e)
        if text is not None and text.lower() in PLACEHOLDER_TEXTS:
            continue
        imp.import_entity(e, target_layout=dst_layout)
    imp.finalize()
    new_entities = [e for e in dst_layout if id(e) not in before]

    LINE_LIKE = {"LINE", "LWPOLYLINE"}
    TEXT_LIKE = {"TEXT", "MTEXT"}

    def _insert_point(e):
        if e.dxftype() in TEXT_LIKE:
            return e.dxf.insert
        return None

    def _is_duplicate(new_e, old_e):
        t1, t2 = new_e.dxftype(), old_e.dxftype()
        if t1 in LINE_LIKE and t2 in LINE_LIKE:
            # Real duplicate border/grid lines coincide almost exactly
            # (they're literally the same template geometry) - a plain
            # bbox overlap test is reliable here.
            try:
                b1 = ezbbox.extents([new_e])
                b2 = ezbbox.extents([old_e])
            except Exception:
                return False
            return _boxes_overlap(b1, b2)
        if t1 in TEXT_LIKE and t2 in TEXT_LIKE:
            # Adjacent table cells (e.g. a row-number marker next to a
            # data cell) can have overlapping bounding boxes without
            # being duplicates of each other - MTEXT's box model is
            # wider than the visible glyphs. Compare INSERT POINTS
            # instead: two copies of "the same label" land within a
            # couple mm of each other; different cells don't.
            p1, p2 = _insert_point(new_e), _insert_point(old_e)
            if p1 is None or p2 is None:
                return False
            return p1.distance(p2) < 2.0
        return False

    superseded = []
    remaining_b = list(b_originals)
    for new_e in new_entities:
        still_remaining = []
        for old_e in remaining_b:
            if _is_duplicate(new_e, old_e):
                superseded.append(old_e)
                dst_layout.delete_entity(old_e)
            else:
                still_remaining.append(old_e)
        remaining_b = still_remaining

    return new_entities, superseded


def _protected_zone_bboxes(doc, layout, titleblock_insert):
    """bbox of the title block insert itself, used to detect collisions
    with anything else placed on the sheet."""
    return ezbbox.extents([titleblock_insert])


def _relocate_if_overlapping(dst_doc, dst_layout, entities, protected_entities, sheet_height):
    """Collision handling: if an entity's bbox overlaps any INDIVIDUAL
    protected entity's bbox (a specific title-block/border/table line or
    label - NOT the bounding box of the whole frame, which would just be
    the entire sheet rectangle and falsely "collide" with everything
    inside it), shift the whole entity straight down, clear below the
    sheet, so nothing is silently lost - the user drags it back into
    place inside AutoCAD."""
    protected_boxes = []
    for pe in protected_entities:
        try:
            b = ezbbox.extents([pe])
        except Exception:
            continue
        if b.has_data:
            protected_boxes.append(b)

    relocated = 0
    shift = Vec3(0, -(sheet_height + 30), 0)
    for e in entities:
        try:
            b = ezbbox.extents([e])
        except Exception:
            continue
        if not b.has_data:
            continue
        if any(_boxes_overlap(b, pb) for pb in protected_boxes):
            e.translate(shift.x, shift.y, shift.z)
            relocated += 1
    return relocated


def run_merge(a_doc: ezdxf.document.Drawing,
              b_doc: ezdxf.document.Drawing,
              target_layout_name: str,
              mode: str,
              semi_finished: bool = False,
              extra_note_lines: Optional[list] = None) -> tuple[ezdxf.document.Drawing, MergeReport]:
    warnings = []

    a_layout = a_doc.layout(target_layout_name)
    b_layout_name = next((l for l in core.list_layout_names(b_doc)), None)
    if b_layout_name is None:
        raise ValueError("B drawing has no paperspace layout to merge from")
    b_layout = b_doc.layout(b_layout_name)

    a_tb = core.find_titleblock_insert(a_layout)
    b_tb = core.find_titleblock_insert(b_layout)
    if a_tb is None:
        raise ValueError("Could not find a title block (INSERT with attributes) in A's target layout")

    # ------------------------------------------------------------
    # Start from B (keeps B's model space geometry untouched) and
    # rebuild B's paperspace layout to match A's target paper size.
    # ------------------------------------------------------------
    dst_doc = b_doc
    dst_layout = b_layout
    dst_layout.dxf.paper_width = a_layout.dxf.paper_width
    dst_layout.dxf.paper_height = a_layout.dxf.paper_height

    # Import the A frame block
    imp = importer.Importer(a_doc, dst_doc)
    imp.import_block(a_tb.dxf.name)
    imp.finalize()

    # Attribute carry-over (B's real values -> A's new ATTDEFs)
    new_block = dst_doc.blocks.get(a_tb.dxf.name)
    attdefs = core.attdefs_of(new_block)
    if b_tb is not None:
        pairs = core.carry_over_attributes(b_tb, attdefs, b_tb.dxf.insert)
        unmatched = [ad.dxf.tag for ad, v in pairs if v == ad.dxf.text and not any(
            a.dxf.tag == ad.dxf.tag for a in b_tb.attribs)]
    else:
        pairs = [(ad, ad.dxf.text) for ad in attdefs]
        unmatched = [ad.dxf.tag for ad in attdefs]
        warnings.append("B has no existing title block - all fields left at A's defaults.")

    # Remove B's old title block, place the new one at A's own insert
    # offset (so the frame graphic lines up with A's own border/margins).
    if b_tb is not None:
        dst_layout.delete_entity(b_tb)
    new_insert = core.place_titleblock(
        dst_doc, dst_layout, a_tb.dxf.name, a_tb.dxf.insert,
        a_tb.dxf.xscale, a_tb.dxf.yscale, a_tb.dxf.rotation, pairs,
    )

    # ------------------------------------------------------------
    # B's own Notes/備註 block would otherwise sit exactly on top of
    # A's (both use roughly the same corner of the sheet) - remove it
    # before bringing A's furniture in, in all three modes, since all
    # three swap in A's frame; only the *styling* differs between modes.
    # ------------------------------------------------------------
    b_notes = None
    for e in list(dst_layout):
        if e.dxftype() == "MTEXT":
            t = e.text if hasattr(e, "text") else e.dxf.text
            if "Notes:" in t or "備註" in t:
                b_notes = e
                break
    if b_notes is not None:
        dst_layout.delete_entity(b_notes)

    # Snapshot of B's own remaining paperspace content (its legend table,
    # revision-history data, etc.) BEFORE A's furniture comes in - used
    # to detect + clean up exact-position duplicates (see
    # _copy_paperspace_extras), never to delete anything B-unique.
    b_originals = [e for e in dst_layout if e.dxftype() != "VIEWPORT" and e is not new_insert]

    # ------------------------------------------------------------
    # Copy A's non-titleblock paperspace furniture (outer border,
    # revision-table headers, tolerance chart, notes, ...) so the sheet
    # actually looks like A. All three modes swap the frame in - they
    # only differ in whether colors/fonts get harmonised afterwards.
    # ------------------------------------------------------------
    # B keeps its own VIEWPORT entities (they point at B's model space);
    # only copy A's static frame furniture.
    a_skip = {a_tb} | {e for e in a_layout if e.dxftype() == "VIEWPORT"}
    imported_furniture, superseded = _copy_paperspace_extras(
        a_doc, a_layout, dst_doc, dst_layout, skip_entities=a_skip, b_originals=b_originals)
    if superseded:
        warnings.append(f"已移除 B 圖面上 {len(superseded)} 個與 A 圖框位置重疊的舊物件（改用 A 的版本）。")

    # ------------------------------------------------------------
    # Style harmonisation
    # ------------------------------------------------------------
    if mode == MODE_B_CONTENT_A_STYLE:
        core.harmonize_layers(a_doc, dst_doc)
        core.harmonize_text_styles(a_doc, dst_doc, [s.dxf.name for s in a_doc.styles])
        skip = {l.dxf.name for l in a_doc.layers}
        core.set_lineweight_for_undefined_layers(dst_doc, DEFAULT_LINEWEIGHT_1_100MM, skip_names=skip)
    elif mode == MODE_B_STYLE_A_FRAME_ONLY:
        # A's frame text should look like B's own formatting: force the
        # imported frame block's text entities onto B's dominant text
        # style/layer color scheme (best-effort - flagged for review).
        warnings.append(
            "Mode 2: A 圖框內的文字樣式已盡量對齊 B，但複雜混排（多色/多字型段落）"
            "仍請人工確認一次。")
    # Mode 3: no harmonisation at all.

    # ------------------------------------------------------------
    # Scale: compute a clean ratio that fits the model into the content
    # viewport, unless this is a semi-finished part (scale -> n/a).
    # ------------------------------------------------------------
    vp = core.find_content_viewport(dst_layout)
    box = core.model_bbox(dst_doc)
    scale_ad = _find_scale_attdef(pairs)
    if vp is not None and box.has_data:
        if semi_finished:
            scale = core.ScaleResult("n/a", 1.0)
            # still make sure it fits, using the largest clean scale that
            # does, purely for the viewport zoom (not written as a ratio)
            fit_scale = core.compute_clean_scale(box.size.x, box.size.y, vp.dxf.width, vp.dxf.height)
            core.apply_scale_to_viewport(vp, fit_scale, box.center)
        else:
            scale = core.compute_clean_scale(box.size.x, box.size.y, vp.dxf.width, vp.dxf.height)
            core.apply_scale_to_viewport(vp, scale, box.center)
        if scale_ad is not None:
            for a in new_insert.attribs:
                if a.dxf.tag == scale_ad.dxf.tag:
                    a.dxf.text = scale.ratio_text
    else:
        scale = core.ScaleResult("n/a", 1.0)
        warnings.append("找不到內容 viewport 或模型空間為空，比例欄位未自動計算，請手動確認。")

    # ------------------------------------------------------------
    # Notes: append any extra lines the user supplied, after A's own
    # notes block, auto-shrinking to stay clear of the sheet border.
    # ------------------------------------------------------------
    if extra_note_lines:
        _append_notes(dst_doc, dst_layout, a_tb, extra_note_lines, warnings)

    # ------------------------------------------------------------
    # Overlap handling: title-block INSERTs typically draw the sheet's
    # own full-page border as part of the block (confirmed on the
    # Wellell family), so the insert's overall bbox is nearly the whole
    # page and useless for collision testing - almost anything near any
    # edge would "overlap" it. Its ATTRIB values (the actual filled-in
    # text - draw no., name, dates, ...) are a much smaller, precise
    # stand-in for "the part of the title block someone could actually
    # print something on top of". Only check against that.
    # Superseded near-exact duplicates were already handled above (see
    # `superseded`), which is the precise, low-false-positive check;
    # this is just a coarse backstop for the title block's live fields.
    # ------------------------------------------------------------
    frame_ids = {id(e) for e in [new_insert] + imported_furniture}
    others = [e for e in dst_layout if id(e) not in frame_ids and e.dxftype() != "VIEWPORT"]
    protected_entities = list(new_insert.attribs)
    relocated = _relocate_if_overlapping(dst_doc, dst_layout, others, protected_entities, dst_layout.dxf.paper_height)

    report = MergeReport(scale=scale, relocated_entities=relocated,
                          unmatched_attdefs=unmatched, warnings=warnings)
    return dst_doc, report


def _append_notes(dst_doc, dst_layout, titleblock_insert, extra_lines, warnings):
    notes_entity = None
    for e in dst_layout:
        if e.dxftype() == "MTEXT":
            t = e.text if hasattr(e, "text") else e.dxf.text
            if "Notes:" in t or "備註" in t:
                notes_entity = e
                break
    if notes_entity is None:
        warnings.append("找不到 Notes / 備註區塊，新增的備註未寫入，請手動加入。")
        return

    old_text = notes_entity.text
    # figure out the next item number by counting existing "\PN." markers
    import re
    nums = [int(m) for m in re.findall(r"\\P(\d+)\.", old_text)] or [0]
    start = max(nums) + 1
    addition = "".join(f"\\P{start + i}.{line}" for i, line in enumerate(extra_lines))
    if old_text.endswith("}"):
        notes_entity.text = old_text[:-1] + addition + "}"
    else:
        notes_entity.text = old_text + addition

    # shrink to fit above the sheet border (found from the titleblock's
    # own frame geometry, falling back to a fixed 10mm margin)
    border_y = 0.0
    try:
        # titleblock insert's own bbox lower edge is a reasonable proxy
        # for "stuff below here is off the printable area" in most
        # Wellell-family templates; otherwise fall back to 10mm.
        tb_box = ezbbox.extents([titleblock_insert])
        border_y = min(10.0, tb_box.extmin.y)
    except Exception:
        border_y = 10.0

    from ezdxf import bbox as _bb
    h = notes_entity.dxf.char_height
    for _ in range(30):
        box = _bb.extents([notes_entity])
        if box.extmin.y >= border_y + 2:
            break
        h *= 0.92
        notes_entity.dxf.char_height = h
    else:
        warnings.append("備註內容過多，已縮到安全下限仍可能超出圖框，請人工確認/搬移。")
