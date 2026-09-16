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
import re
from difflib import SequenceMatcher
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


def _translate_entity(e, dx, dy, dz, warnings):
    """Shift one paperspace entity by (dx, dy, dz). Most DXF entity types
    support the generic .translate(), but a few (VIEWPORT most notably)
    raise NotImplementedError from ezdxf's own transform machinery and
    need their anchor attribute nudged by hand."""
    if dx == 0 and dy == 0 and dz == 0:
        return True
    try:
        e.translate(dx, dy, dz)
        return True
    except NotImplementedError:
        pass
    if e.dxftype() == "VIEWPORT":
        c = e.dxf.center
        e.dxf.center = Vec3(c.x + dx, c.y + dy, c.z)
        return True
    warnings.append(f"無法自動平移 {e.dxftype()} 物件對齊新圖框，請人工確認其位置。")
    return False


def _is_notes_entity(e) -> bool:
    """True for the Notes/備註 MTEXT block, matched case-insensitively
    since real drawings mix "Notes:", "NOTES:", "Note:" and "備註" -
    a case-sensitive check here previously missed all-caps "NOTES:"
    blocks entirely, leaving them un-merged AND un-deleted (duplicated
    on top of A's own notes)."""
    if e.dxftype() != "MTEXT":
        return False
    t = e.text if hasattr(e, "text") else e.dxf.text
    t_upper = t.upper()
    return "NOTES:" in t_upper or "NOTE:" in t_upper or "備註" in t


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


# ---------------------------------------------------------------------
# Notes / 備註 combination
#
# The manually-validated process never blindly appends text: it (1) reads
# B's own Notes items, (2) for each one checks whether A's standard notes
# already say essentially the same thing - if so A's wording wins and B's
# version is dropped ("如有類似字詞就直接取代"), (3) whatever is left
# over from B (plus anything the user additionally typed in) gets
# appended after A's items, continuing the numbering ("接續在...之下").
# This block implements that matching + renumbering generically, instead
# of only appending manually-typed extra lines while ignoring B's actual
# Notes content (the bug reported against the first generalised build).
# ---------------------------------------------------------------------

_NUM_ITEM_RE = re.compile(r"^(\d+)\.(.*)", re.S)
_CTRL_CODE_RE = re.compile(r"\\[A-Za-z][^;\\]*;")

NOTE_DUPLICATE_THRESHOLD = 0.30
NOTE_BORDERLINE_THRESHOLD = 0.15


def _parse_note_items(raw_text: str):
    """Split a Notes/備註 MTEXT's raw .text into (header, items, close).

    items is a list of {"num": int, "paras": [str, ...]} where paras[0]
    is the "<N>.<body>" paragraph and any following paras are
    continuation lines (e.g. an indented translation) that belong to the
    same item, up to the next "<N>." paragraph.
    """
    text = raw_text or ""
    close = ""
    if text.endswith("}"):
        text = text[:-1]
        close = "}"
    paras = text.split("\\P")
    header = paras[0] if paras else ""
    items = []
    current = None
    for para in paras[1:]:
        m = _NUM_ITEM_RE.match(para)
        if m:
            if current:
                items.append(current)
            current = {"num": int(m.group(1)), "paras": [para]}
        elif current is not None:
            current["paras"].append(para)
        else:
            # stray content before any numbered item - keep it, don't
            # silently drop it, by folding it into the header.
            header += "\\P" + para
    if current:
        items.append(current)
    return header, items, close


def _normalize_note_item(item) -> str:
    joined = " ".join(item["paras"])
    joined = _CTRL_CODE_RE.sub(" ", joined)
    joined = re.sub(r"^\s*\d+\.\s*", "", joined)
    joined = re.sub(r"[\s\W]+", "", joined, flags=re.UNICODE)
    return joined.lower()


def _note_item_similarity(item_a, item_b) -> float:
    na, nb = _normalize_note_item(item_a), _normalize_note_item(item_b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _combine_notes_text(a_raw: str, b_raw: Optional[str], extra_lines, warnings) -> str:
    """Return A's Notes text with B's genuinely-new items (and any
    manually-typed extra lines) appended, continuing A's numbering; items
    that are near-duplicates of something A already says are dropped so
    A's standard wording wins, matching the validated manual behaviour."""
    a_header, a_items, a_close = _parse_note_items(a_raw)
    close = a_close or "}"

    new_item_paras = []  # list of list[str] (paragraphs), first para starts "<n>."
    if b_raw:
        _, b_items, _ = _parse_note_items(b_raw)
        for b_item in b_items:
            if a_items:
                score, _matched = max(
                    ((_note_item_similarity(b_item, a_item), a_item) for a_item in a_items),
                    key=lambda t: t[0],
                )
            else:
                score = 0.0
            if score >= NOTE_DUPLICATE_THRESHOLD:
                continue  # A's existing item already says this - A wins
            if score >= NOTE_BORDERLINE_THRESHOLD:
                preview = "".join(b_item["paras"])[:30]
                warnings.append(
                    f"舊圖備註第{b_item['num']}項與 A 圖某項備註用詞有些相似（但未達自動去重門檻），"
                    f"已當作新項目保留，請確認是否重複：{preview}")
            new_item_paras.append(b_item["paras"])

    for line in (extra_lines or []):
        new_item_paras.append([f"@@N@@.{line}"])  # placeholder number fixed below

    if not new_item_paras:
        return a_raw or (a_header + close)

    start = (max((it["num"] for it in a_items), default=0)) + 1
    rebuilt = []
    for i, paras in enumerate(new_item_paras):
        n = start + i
        first = re.sub(r"^(@@N@@|\d+)\.", f"{n}.", paras[0])
        rebuilt.append(first)
        rebuilt.extend(paras[1:])

    all_a_paras = [p for it in a_items for p in it["paras"]]
    combined_body = "\\P".join([a_header] + all_a_paras + rebuilt)
    return combined_body + close


def _merge_notes_content(dst_layout, notes_entity, new_insert, mode,
                          b_notes_snapshot, extra_lines, warnings):
    """Combine A's (already-imported) Notes text with B's original Notes
    content, then make sure the result fits inside the frame by moving
    the block (never by shrinking the font - font size follows whichever
    side's format the selected mode says should win)."""
    a_raw = notes_entity.text if hasattr(notes_entity, "text") else notes_entity.dxf.text
    b_raw = b_notes_snapshot["text"] if b_notes_snapshot else None
    combined = _combine_notes_text(a_raw, b_raw, extra_lines, warnings)
    notes_entity.text = combined

    if mode == MODE_B_STYLE_A_FRAME_ONLY and b_notes_snapshot:
        # Mode 2: B's formatting wins everywhere, including the notes
        # block's own char size/style/layer/color.
        for attr in ("char_height", "style", "layer", "color"):
            if attr in b_notes_snapshot:
                setattr(notes_entity.dxf, attr, b_notes_snapshot[attr])

    _fit_notes_in_frame(dst_layout, notes_entity, new_insert, warnings)


def _fit_notes_in_frame(dst_layout, notes_entity, new_insert, warnings):
    """If the (now possibly longer) Notes block extends below the sheet's
    printable border, move it up so it clears the border - do NOT shrink
    the character height to make it fit."""
    try:
        tb_box = ezbbox.extents([new_insert])
        border_y = min(10.0, tb_box.extmin.y) if tb_box.has_data else 10.0
    except Exception:
        border_y = 10.0

    box = ezbbox.extents([notes_entity])
    if not box.has_data:
        return
    margin = 2.0
    overflow = (border_y + margin) - box.extmin.y
    if overflow <= 0:
        return

    notes_entity.translate(0, overflow, 0)

    # Backstop: after moving it up, make sure it didn't just land on top
    # of something else (e.g. a table hugging the top of the sheet) -
    # if so, flag for manual placement rather than guessing further.
    new_box = ezbbox.extents([notes_entity])
    for other in dst_layout:
        if other is notes_entity:
            continue
        if other.dxftype() == "VIEWPORT":
            continue
        try:
            ob = ezbbox.extents([other])
        except Exception:
            continue
        if ob.has_data and _boxes_overlap(new_box, ob):
            warnings.append(
                "備註內容較多，已將備註區塊往圖框內上移以避免超出邊界，"
                "但可能與其他內容重疊，請人工確認/微調位置（未自動縮小字級）。")
            break


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

    # ------------------------------------------------------------
    # A's title block is placed at A's OWN insert point, which is not
    # necessarily where B's old title block sat - different template
    # families use different paper sizes/margins for the "same" corner.
    # Everything else B already has on this sheet (its notes, its legend
    # table, revision headers, viewports, ...) was positioned relative to
    # B's OLD frame, so it needs to move by the same offset or it ends up
    # floating disconnected from the new frame (this was the root cause
    # behind "old tables not integrated" on a second, differently-sized
    # template family: the frame swap moved the border but left
    # everything else at B's old coordinates).
    # ------------------------------------------------------------
    if b_tb is not None:
        frame_offset = Vec3(
            a_tb.dxf.insert.x - b_tb.dxf.insert.x,
            a_tb.dxf.insert.y - b_tb.dxf.insert.y,
            0.0,
        )
        if frame_offset.x or frame_offset.y:
            for e in list(dst_layout):
                if e is b_tb:
                    continue
                _translate_entity(e, frame_offset.x, frame_offset.y, 0.0, warnings)

    # Import the A frame block. A and B can legitimately use the exact
    # same block NAME for their title block (e.g. both call it "A4-H" -
    # confirmed on the Y575A02 test file) - Importer.import_block() then
    # auto-renames the incoming one (e.g. to "A4-H0") rather than
    # overwriting B's old definition, and returns whichever name it
    # actually used. That returned name - not A's original name string -
    # is what must be inserted, or the "new" title block ends up being a
    # blockref to B's untouched OLD block definition (looks exactly like
    # the old frame was never replaced).
    imp = importer.Importer(a_doc, dst_doc)
    new_block_name = imp.import_block(a_tb.dxf.name)
    imp.finalize()

    # Attribute carry-over (B's real values -> A's new ATTDEFs)
    new_block = dst_doc.blocks.get(new_block_name)
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
    old_block_name = b_tb.dxf.name if b_tb is not None else None
    if b_tb is not None:
        dst_layout.delete_entity(b_tb)
    new_insert = core.place_titleblock(
        dst_doc, dst_layout, new_block_name, a_tb.dxf.insert,
        a_tb.dxf.xscale, a_tb.dxf.yscale, a_tb.dxf.rotation, pairs,
    )

    # If B's old title block had the same block NAME as A's (a name
    # collision, not the same content), that old block definition is now
    # unreferenced - remove it so it isn't left behind as dead, confusing
    # data in the output file.
    if old_block_name is not None and old_block_name != new_block_name:
        try:
            dst_doc.blocks.delete_block(old_block_name, safe=True)
        except Exception:
            pass

    # ------------------------------------------------------------
    # B's own Notes/備註 block would otherwise sit exactly on top of
    # A's (both use roughly the same corner of the sheet) - remove it
    # before bringing A's furniture in, in all three modes, since all
    # three swap in A's frame; only the *styling* differs between modes.
    # ------------------------------------------------------------
    b_notes = None
    for e in list(dst_layout):
        if _is_notes_entity(e):
            b_notes = e
            break
    b_notes_snapshot = None
    if b_notes is not None:
        b_notes_snapshot = {
            "text": b_notes.text if hasattr(b_notes, "text") else b_notes.dxf.text,
            "char_height": b_notes.dxf.char_height,
            "style": b_notes.dxf.style,
            "layer": b_notes.dxf.layer,
            "color": b_notes.dxf.color,
        }
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
    # Notes: combine A's standard notes with whatever B's own Notes block
    # actually said (dropping B items that just restate something A
    # already says, keeping B's genuinely new ones, continuing the
    # numbering), plus any extra lines the user typed in. If the result
    # no longer fits above the sheet border, move the block up - the
    # font size is never auto-shrunk.
    # ------------------------------------------------------------
    notes_entity = None
    for e in dst_layout:
        if _is_notes_entity(e):
            notes_entity = e
            break
    if notes_entity is not None:
        _merge_notes_content(dst_layout, notes_entity, new_insert, mode,
                              b_notes_snapshot, extra_note_lines, warnings)
    elif b_notes_snapshot or extra_note_lines:
        warnings.append("找不到 A 圖框的 Notes / 備註區塊，舊圖備註與新增備註未寫入，請手動加入。")

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
