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
    support the generic .translate(), but a few raise NotImplementedError
    from ezdxf's own transform machinery and need their position nudged
    by hand:
      - VIEWPORT: position lives in dxf.center.
      - OLE2FRAME: an embedded OLE object (e.g. a pasted Excel BOM table
        - confirmed on the Y575A02 test file, whose "BOM表" turned out to
        be exactly this) has no ezdxf-level position attribute at all;
        its corner points are two raw group-code tags (10 and 11) inside
        acdb_ole2frame that have to be rewritten directly. Without this,
        an embedded BOM table silently stayed at B's OLD coordinates
        while everything else moved to align with A's new frame,
        landing it in the wrong spot relative to the new border/viewport
        (reported as "多個BOM表...重疊").
    """
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
    if e.dxftype() == "OLE2FRAME" and getattr(e, "acdb_ole2frame", None) is not None:
        # These corner points are stored as DXFVertex tags (group codes
        # 10/11, each expanding to three 10/20/30-style lines on export)
        # - replacing one with a plain DXFTag silently corrupts the file
        # structure (confirmed: ezdxf then fails to re-read it with
        # "Missing required y coordinate"). AND: the same corners are
        # ALSO duplicated inside the entity's embedded binary OLE stream
        # (group 310) - AutoCAD reads THAT copy, not these plain tags,
        # so both must be updated in lockstep or the move is invisible
        # in real AutoCAD (confirmed against a real screenshot: the
        # plain tags were correctly updated yet the object stayed put).
        # See _patch_ole_binary_corners for the full story.
        from ezdxf.lldxf.types import DXFVertex
        tags = e.acdb_ole2frame
        v10 = tags.get_first_value(10, None)
        v11 = tags.get_first_value(11, None)
        for code, v in ((10, v10), (11, v11)):
            if v is not None:
                tags.set_first(DXFVertex(code, (v[0] + dx, v[1] + dy, v[2] + dz)))
        if v10 is not None and v11 is not None:
            xs = (v10[0] + dx, v11[0] + dx)
            ys = (v10[1] + dy, v11[1] + dy)
            _patch_ole_binary_corners(e, min(xs), min(ys), max(xs), max(ys))
        return True
    warnings.append(f"無法自動平移 {e.dxftype()} 物件對齊新圖框，請人工確認其位置。")
    return False


def _entity_bbox(e):
    """bbox helper that also covers OLE2FRAME (ezbbox.extents() doesn't
    know how to measure it - it has its own .bbox() using the raw corner
    tags)."""
    if e.dxftype() == "OLE2FRAME":
        try:
            return e.bbox()
        except Exception:
            return ezbbox.BoundingBox()
    try:
        return ezbbox.extents([e])
    except Exception:
        return ezbbox.BoundingBox()


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


def _find_clear_offset(box, protected_boxes, no_go_boxes, sheet_width, sheet_height):
    """Try to find a small nudge that clears `box` of every box in
    protected_boxes, AND stays completely outside every box in
    no_go_boxes, while staying on the sheet - preferring to keep the
    content as close as possible to where it already is (its position
    coming from B is deliberate: "優先參照 B 圖原有的位置"), rather than
    always shoving it far away.

    no_go_boxes is meant for content viewports: we cannot see what a
    viewport is actually displaying from model space (leader lines,
    balloon circles, ...), so rather than gamble on "probably empty
    space" inside that rectangle, any candidate touching it is rejected
    outright - confirmed the hard way (a first version nudged a BOM
    table into a viewport's middle, landing on top of balloon leaders).
    If nothing clears both sets on-sheet, the caller falls back to
    moving it below the sheet instead of guessing further.

    Returns (dx, dy) or None if nothing qualifies."""
    w, h = box.size.x, box.size.y
    step_x, step_y = w + 5, h + 5
    candidates = [
        (0, 0),
        (step_x, 0), (-step_x, 0),
        (2 * step_x, 0), (-2 * step_x, 0),
        (0, step_y), (0, -step_y),
        (step_x, step_y), (-step_x, step_y), (step_x, -step_y), (-step_x, -step_y),
    ]
    for dx, dy in candidates:
        test = ezbbox.BoundingBox([
            Vec3(box.extmin.x + dx, box.extmin.y + dy, 0),
            Vec3(box.extmax.x + dx, box.extmax.y + dy, 0),
        ])
        if test.extmin.x < 0 or test.extmin.y < 0 or test.extmax.x > sheet_width or test.extmax.y > sheet_height:
            continue
        if any(_boxes_overlap(test, pb) for pb in protected_boxes):
            continue
        if any(_boxes_overlap(test, nb) for nb in no_go_boxes):
            continue
        return dx, dy
    return None


def _next_clear_outside_dy(test_box, outside_boxes, gap=5.0):
    """Given a candidate box already shifted out to the right of the
    sheet, find a vertical offset (stacking downward in gap-sized
    steps) that clears every box already parked outside the frame this
    run. Two things placed outside the frame independently (e.g. BOM
    and Notes both anchored back to y=0) can easily land on top of each
    other there - "不管是圖框內外的資訊都不能重疊" applies just as much
    outside the frame as inside it, so this is re-checked the same way
    every other collision in this tool is: never assume, always verify
    against what's already there and pull apart if not."""
    dy = 0.0
    h = test_box.size.y
    for _ in range(500):
        shifted = ezbbox.BoundingBox([
            Vec3(test_box.extmin.x, test_box.extmin.y + dy, 0),
            Vec3(test_box.extmax.x, test_box.extmax.y + dy, 0),
        ])
        if not any(_boxes_overlap(shifted, ob) for ob in outside_boxes):
            return dy
        dy -= (h + gap)
    return dy


def _relocate_outside_if_overlapping(entity, protected_boxes, viewport_boxes,
                                      sheet_width, sheet_height, warnings, label=None,
                                      outside_boxes=None):
    """Simplified, explicitly-requested rule for BOM/OLE objects and
    Notes: no auto-resize, no searching for a nearby clear spot on the
    sheet - just two checks. If this entity's bbox overlaps the frame's
    own protected content OR any content viewport (the model view
    area), move the WHOLE entity outside the frame (to the right of the
    sheet border) exactly as-is (same size, same font) and leave it for
    manual placement. If it doesn't overlap anything, leave it exactly
    where it already is.

    `outside_boxes`, when given, is a running list of boxes already
    parked outside the frame this run (by this function or by the
    generic fallback below) - anything new placed outside is checked
    against that list too and stacked downward with a small gap until
    clear, so multiple relocated objects don't just pile up on top of
    each other outside the frame instead of inside it."""
    b = _entity_bbox(entity)
    if not b.has_data:
        return False
    overlaps = (any(_boxes_overlap(b, pb) for pb in protected_boxes)
                or any(_boxes_overlap(b, vb) for vb in viewport_boxes))
    if not overlaps:
        return False
    dx = (sheet_width + 10.0) - b.extmin.x
    dy = 0.0
    if outside_boxes:
        shifted = ezbbox.BoundingBox([
            Vec3(b.extmin.x + dx, b.extmin.y, 0), Vec3(b.extmax.x + dx, b.extmax.y, 0)])
        dy += _next_clear_outside_dy(shifted, outside_boxes)
    _translate_entity(entity, dx, dy, 0.0, warnings)
    name = label or entity.dxftype()
    warnings.append(f"{name} 與圖框內容或模型視圖重疊，已移到圖框外（圖紙右側），請人工搬到合適位置。")
    final_box = ezbbox.BoundingBox([
        Vec3(b.extmin.x + dx, b.extmin.y + dy, 0), Vec3(b.extmax.x + dx, b.extmax.y + dy, 0)])
    if outside_boxes is not None:
        outside_boxes.append(final_box)
    return True


def _relocate_if_overlapping(dst_doc, dst_layout, entities, protected_entities,
                              viewport_boxes, sheet_width, sheet_height, warnings,
                              outside_boxes=None):
    """Collision handling: if an entity's bbox overlaps any INDIVIDUAL
    protected entity's bbox (a specific title-block/border/table line or
    label - NOT the bounding box of the whole frame, which would just be
    the entire sheet rectangle and falsely "collide" with everything
    inside it), first try nudging it sideways/up/down by roughly its own
    size to a nearby clear spot on the same sheet that also stays clear
    of every content viewport (its B-original position is deliberate -
    "優先參照 B 圖原有的位置，如有重疊再做調整，不限制只能在同樣位
    置" - but never into the middle of a view: "寧可移到圖紙下方讓你手
    動歸位，也不要冒險放到視圖中間"). Only if no such on-sheet spot
    exists does it fall back to parking the whole thing outside the
    frame, beside the sheet border, so nothing is silently lost - the
    user drags it back into place inside AutoCAD ("直接就放到圖框外，
    而不是放在圖框內的下方": outside the frame, never guessed into a
    spot still technically inside it).
    """
    protected_boxes = []
    for pe in protected_entities:
        b = _entity_bbox(pe)
        if b.has_data:
            protected_boxes.append(b)

    relocated = 0
    for e in entities:
        b = _entity_bbox(e)
        if not b.has_data:
            continue
        if not any(_boxes_overlap(b, pb) for pb in protected_boxes):
            continue
        offset = _find_clear_offset(b, protected_boxes, viewport_boxes, sheet_width, sheet_height)
        if offset is not None:
            dx, dy = offset
            warnings.append(
                f"{e.dxftype()} 物件原本與圖框內容重疊，已自動移動到附近的空位"
                f"（位移約 {dx:+.0f}, {dy:+.0f} mm，且不在任何視圖範圍內），"
                f"請開圖確認位置是否理想。")
        else:
            dx, dy = (sheet_width + 10.0) - b.extmin.x, 0.0
            if outside_boxes:
                shifted = ezbbox.BoundingBox([
                    Vec3(b.extmin.x + dx, b.extmin.y, 0), Vec3(b.extmax.x + dx, b.extmax.y, 0)])
                dy += _next_clear_outside_dy(shifted, outside_boxes)
            warnings.append(
                f"{e.dxftype()} 物件與圖框內容重疊，圖紙內找不到不會蓋到視圖的空位，"
                f"已移到圖框外（圖紙右側），請人工搬回合適位置（不冒險放到視圖中間）。")
            if outside_boxes is not None:
                outside_boxes.append(ezbbox.BoundingBox([
                    Vec3(b.extmin.x + dx, b.extmin.y + dy, 0),
                    Vec3(b.extmax.x + dx, b.extmax.y + dy, 0)]))
        _translate_entity(e, dx, dy, 0.0, warnings)
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


def _merge_notes_content(notes_entity, mode, b_notes_snapshot, extra_lines, warnings):
    """Combine A's (already-imported) Notes text with B's original Notes
    content. Positioning/fitting is a separate step (see
    _fit_notes_in_frame and _place_notes_in_reserved_band) since it
    depends on whether there's a BOM-like object to share the bottom of
    the sheet with."""
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
        ob = _entity_bbox(other)
        if ob.has_data and _boxes_overlap(new_box, ob):
            warnings.append(
                "備註內容較多，已將備註區塊往圖框內上移以避免超出邊界，"
                "但可能與其他內容重疊，請人工確認/微調位置（未自動縮小字級）。")
            break


# ---------------------------------------------------------------------
# Making room for a BOM-like object (e.g. an embedded OLE Excel table)
# and Notes to share the bottom of the sheet with the model view,
# instead of leaving it to chance where the model happens to have been
# drawn. This is only used when B actually has such an object - a plain
# part drawing (no BOM/OLE content) keeps the original, simpler
# behaviour (centered viewport, notes moved up only, never shrunk).
#
# The approach mirrors what a human does in AutoCAD: pan/zoom the
# viewport's camera so a strip along the bottom of the sheet is left
# genuinely empty of model geometry (never move the model itself), then
# place the BOM table and Notes inside that guaranteed-empty strip.
# ---------------------------------------------------------------------

def _title_table_char_height_floor(new_insert) -> float:
    """Smallest ATTRIB character height in the title block - the floor
    Notes should never be shrunk below ("最小字級不小於圖框表格的文
    字")."""
    heights = [a.dxf.height for a in new_insert.attribs if a.dxf.height and a.dxf.height > 0]
    return min(heights) if heights else 1.5


def _attribs_cluster_bbox(new_insert):
    try:
        return ezbbox.extents(list(new_insert.attribs))
    except Exception:
        return ezbbox.BoundingBox()


def _fit_scale_with_bottom_margin(model_w, model_h, avail_w, avail_h,
                                   reserved_margin_mm, min_top_margin_mm=5.0):
    """Like core.compute_clean_scale, but when reserved_margin_mm > 0
    also requires enough headroom to leave that much space empty at the
    bottom (plus a small top margin) once the model is drawn - walking
    to a smaller clean scale (zooming out further) if the tightest fit
    doesn't leave enough room. Returns (ScaleResult, achieved: bool)."""
    if reserved_margin_mm <= 0:
        return core.compute_clean_scale(model_w, model_h, avail_w, avail_h), True
    for n in core.CLEAN_SCALE_STEPS:
        factor = 1.0 / n
        draw_w, draw_h = model_w * factor, model_h * factor
        if draw_w > avail_w or draw_h > avail_h:
            continue
        headroom_h = avail_h - draw_h
        if headroom_h >= reserved_margin_mm + min_top_margin_mm:
            return core.ScaleResult(f"1:{n}", factor), True
    # Couldn't satisfy the reservation at any clean scale that still
    # fits - fall back to the ordinary best-fit and let the caller warn.
    return core.compute_clean_scale(model_w, model_h, avail_w, avail_h), False


def _apply_scale_reserving_bottom(vp, scale, model_box, reserved_margin_mm):
    """Zoom/pan the viewport's camera (never the model geometry) so
    `reserved_margin_mm` of physical space at the bottom of the viewport
    rectangle is left empty of model content."""
    factor = scale.factor
    view_height = vp.dxf.height / factor
    bottom_gap_world = reserved_margin_mm / factor
    vp.dxf.view_height = view_height
    cy = model_box.extmin.y - bottom_gap_world + view_height / 2
    vp.dxf.view_center_point = Vec3(model_box.center.x, cy, 0.0)


def _safe_bottom_band(vp, reserved_margin_mm):
    """The paperspace rectangle at the bottom of the content viewport
    that _apply_scale_reserving_bottom guaranteed is empty of model
    content - safe to place Notes/BOM content inside."""
    x0 = vp.dxf.center.x - vp.dxf.width / 2
    x1 = vp.dxf.center.x + vp.dxf.width / 2
    y0 = vp.dxf.center.y - vp.dxf.height / 2
    y1 = y0 + reserved_margin_mm
    return ezbbox.BoundingBox([Vec3(x0, y0, 0), Vec3(x1, y1, 0)])


def _ole_natural_size(ole):
    box = ole.bbox()
    return box.size.x, box.size.y


def _patch_ole_binary_corners(ole, xmin, ymin, xmax, ymax):
    """The OLE2FRAME's position/size is NOT solely defined by the plain
    group-code tags (10/11) - it is ALSO duplicated inside the entity's
    own embedded binary OLE stream (group 310, the actual compound-file
    bytes): the first ~98 bytes there are a 2-byte header value followed
    by the frame's four corners (upper-left, upper-right, lower-right,
    lower-left) as little-endian doubles. Real AutoCAD reads THIS
    embedded copy, not the plain tags - confirmed two ways: (1) directly
    decoding this drawing's own binary blob showed those four corners
    exactly matching the 10/11 tags at the object's ORIGINAL position,
    and (2) a version of this tool that only edited 10/11 was tested by
    the user in real AutoCAD and the object visibly stayed at its old
    position/size despite the tags (and ezdxf's own bbox() reading of
    them) being correct. Editing 10/11 alone is therefore not enough;
    this embedded copy has to be kept in sync or AutoCAD ignores the
    change entirely. Returns True if the binary copy was found and
    patched, False if this OLE object has no (or too-short) binary data
    to patch (10/11 are still updated by the caller either way)."""
    import struct
    from ezdxf.lldxf.types import DXFBinaryTag
    tags = ole.acdb_ole2frame
    idxs = [i for i, t in enumerate(tags) if t.code == 310]
    if not idxs:
        return False
    chunk_size = len(tags[idxs[0]].value)
    data = b"".join(tags[i].value for i in idxs)
    if len(data) < 98 or chunk_size <= 0:
        return False
    mystery = data[0:2]
    new_header = (mystery
                  + struct.pack("<ddd", xmin, ymax, 0.0)   # upper-left
                  + struct.pack("<ddd", xmax, ymax, 0.0)   # upper-right
                  + struct.pack("<ddd", xmax, ymin, 0.0)   # lower-right
                  + struct.pack("<ddd", xmin, ymin, 0.0))  # lower-left
    new_data = new_header + data[98:]
    new_chunks = [new_data[i:i + chunk_size] for i in range(0, len(new_data), chunk_size)]
    first = idxs[0]
    for i in reversed(idxs):
        del tags[i]
    for offset, chunk in enumerate(new_chunks):
        tags.insert(first + offset, DXFBinaryTag(310, chunk))
    return True


def _set_ole_rect(ole, xmin, ymin, xmax, ymax):
    """Resize/reposition an embedded OLE object (e.g. a pasted Excel BOM
    table) by rewriting its two corner points directly - ezdxf has no
    high-level API for this (translate()/scale() both raise
    NotImplementedError for OLE2FRAME), and the corners are stored as
    DXFVertex tags (group codes 10/11) that must keep that exact tag
    type or the file becomes unreadable (a plain DXFTag replacement was
    confirmed to corrupt it). Also patches the duplicate copy of these
    corners embedded in the binary OLE stream itself - see
    _patch_ole_binary_corners - which is the copy AutoCAD actually
    honours."""
    from ezdxf.lldxf.types import DXFVertex
    tags = ole.acdb_ole2frame
    tags.set_first(DXFVertex(10, (xmin, ymax, 0.0)))
    tags.set_first(DXFVertex(11, (xmax, ymin, 0.0)))
    _patch_ole_binary_corners(ole, xmin, ymin, xmax, ymax)


def _place_bom_objects(dst_doc, dst_layout, ole_objects, new_insert, band,
                        protected_boxes, sheet_width, sheet_height, warnings,
                        placed_boxes_out=None):
    """Place each embedded BOM-like OLE object flush above the title
    block's own field cluster, in the bottom-right corner by default.
    Falls back to the other 3 corners of the safe band, and if none of
    those clear the title block fields either, gives up and parks it
    outside the frame to the right of the sheet border - never inside
    the frame overlapping something, and never guessed into the middle
    of the model view.

    IMPORTANT: this only *repositions* the OLE object, at its original
    (natural) size - it does not resize it. Editing an OLE2FRAME's
    corner tags (group codes 10/11) does change what ezdxf itself
    reports as that entity's bbox, but real AutoCAD was confirmed (by
    directly comparing a delivered file's rendered size against its
    frame tags) to keep displaying embedded Excel/OLE objects at their
    original size regardless of what the DXF frame says - only the
    anchor position is respected, not a shrink/enlarge. So attempting to
    resize here would silently fail in AutoCAD while making the ezdxf
    side of the file report a size that isn't real. Reserving enough
    room for the *natural* size (done by the caller before this runs)
    and only moving it is the approach that's actually reliable."""
    cluster = _attribs_cluster_bbox(new_insert)
    margin = 3.0

    for ole in ole_objects:
        w0, h0 = _ole_natural_size(ole)
        if w0 <= 0 or h0 <= 0:
            continue

        target_w, target_h = w0, h0
        placed = False
        if cluster.has_data:
            corners = [
                # bottom-right, flush above the field cluster (default)
                (cluster.extmax.x - target_w, cluster.extmax.y + margin),
                # bottom-left of the sheet, same idea mirrored
                (band.extmin.x + margin, cluster.extmax.y + margin),
                # top-right / top-left of the safe band itself
                (band.extmax.x - margin - target_w, band.extmax.y - margin - target_h),
                (band.extmin.x + margin, band.extmax.y - margin - target_h),
            ]
            for x0, y0 in corners:
                rect = ezbbox.BoundingBox([Vec3(x0, y0, 0), Vec3(x0 + target_w, y0 + target_h, 0)])
                if rect.extmin.x < 0 or rect.extmin.y < 0 or rect.extmax.x > sheet_width or rect.extmax.y > sheet_height:
                    continue
                if any(_boxes_overlap(rect, pb) for pb in protected_boxes):
                    continue
                _set_ole_rect(ole, rect.extmin.x, rect.extmin.y, rect.extmax.x, rect.extmax.y)
                warnings.append(
                    f"{ole.dxftype()} 物件已移動到圖框右下角表格上方（原始大小 "
                    f"{target_w:.0f}x{target_h:.0f}mm，未縮放 - AutoCAD 對 OLE 物件"
                    f"的顯示大小不會依 DXF 座標縮放，只能維持原始大小移動位置；"
                    f"如果太大，請在 AutoCAD 內手動拖曳縮小一次再存檔），"
                    f"請開圖確認位置是否理想。")
                placed = True
                if placed_boxes_out is not None:
                    placed_boxes_out.append(rect)
                break

        if not placed:
            # Nothing inside the frame worked - park it outside the
            # frame, beside the sheet border, rather than guess further
            # or leave it overlapping something ("寧可移到圖紙下方讓你
            # 手動歸位，也不要冒險放到視圖中間" - and specifically
            # outside the frame, not tucked under it, per the user's
            # own corrected example).
            x0 = sheet_width + 10.0
            y0 = max(0.0, band.extmin.y)
            _set_ole_rect(ole, x0, y0, x0 + target_w, y0 + target_h)
            warnings.append(
                f"{ole.dxftype()} 物件（原始大小 {target_w:.0f}x{target_h:.0f}mm）"
                f"在圖框內找不到不會重疊的位置，已移到圖框外（圖紙右側），"
                f"請人工搬到合適位置（如果太大，也可以先在 AutoCAD 內縮小後再搬回圖框內）。")


def _place_notes_in_band(notes_entity, band, floor_char_height, protected_boxes, warnings,
                          avoid_boxes=None):
    """Position Notes at the bottom-left of the safe band. If it doesn't
    fit at its current font size, shrink it - but never below the title
    block's own smallest field text size - then position it; if it still
    doesn't fit even at the floor size, leave it at the floor size and
    warn rather than shrink further or let it overlap the model.

    `avoid_boxes` (e.g. a BOM/OLE object already placed in the same
    band) narrows the MTEXT's wrap width so it doesn't run out under
    something placed to its right - a plain translate can't fix a
    horizontal overlap the way it can fix a vertical one, since Notes
    reads left-to-right from a fixed anchor."""
    left_x = band.extmin.x + 2.0
    bottom_y = band.extmin.y + 2.0
    avail_h = band.size.y - 4.0

    box = ezbbox.extents([notes_entity])
    if not box.has_data:
        return

    # If something else (typically the BOM/OLE object) already occupies
    # part of this band to the right of where Notes starts, re-wrap
    # Notes narrower so it stops before that object instead of running
    # under it.
    if avoid_boxes:
        obstacles_to_right = [b for b in avoid_boxes if b.extmax.x > left_x]
        if obstacles_to_right:
            right_limit = min(b.extmin.x for b in obstacles_to_right) - 3.0
            if right_limit > left_x + 10.0:  # keep at least some usable width
                new_width = right_limit - left_x
                if notes_entity.dxf.width <= 0 or notes_entity.dxf.width > new_width:
                    notes_entity.dxf.width = new_width
                    box = ezbbox.extents([notes_entity])

    natural_h = box.size.y
    h0 = notes_entity.dxf.char_height

    if natural_h > avail_h and avail_h > 0:
        target_h = h0 * (avail_h / natural_h)
        if target_h < floor_char_height:
            target_h = floor_char_height
            warnings.append(
                f"備註內容較多，字級已縮小到與圖框表格文字相同的下限"
                f"（{floor_char_height:.1f}mm）仍可能超出預留空間，請人工確認。")
        notes_entity.dxf.char_height = target_h
        box = ezbbox.extents([notes_entity])

    # Anchor at the band's bottom-left corner.
    dx = left_x - box.extmin.x
    dy = bottom_y - box.extmin.y
    notes_entity.translate(dx, dy, 0)

    final_box = ezbbox.extents([notes_entity])
    if final_box.extmax.y > band.extmax.y + 0.5:
        warnings.append(
            "備註內容即使縮到圖框表格文字大小的下限，仍超出預留空間，"
            "可能與模型或其他物件重疊，請人工確認/微調位置。")
    for pb in list(protected_boxes) + list(avoid_boxes or []):
        if _boxes_overlap(final_box, pb):
            warnings.append("備註區塊與圖框內容（含 BOM）重疊，請人工確認/微調位置。")
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
    # Notes: combine A's standard notes with whatever B's own Notes block
    # actually said (dropping B items that just restate something A
    # already says, keeping B's genuinely new ones, continuing the
    # numbering), plus any extra lines the user typed in. Done before the
    # scale/viewport step below so its natural (unpositioned) height can
    # inform how much room to reserve at the bottom of the sheet.
    # ------------------------------------------------------------
    notes_entity = None
    for e in dst_layout:
        if _is_notes_entity(e):
            notes_entity = e
            break
    if notes_entity is not None:
        _merge_notes_content(notes_entity, mode, b_notes_snapshot, extra_note_lines, warnings)
    elif b_notes_snapshot or extra_note_lines:
        warnings.append("找不到 A 圖框的 Notes / 備註區塊，舊圖備註與新增備註未寫入，請手動加入。")

    ole_objects = [e for e in dst_layout if e.dxftype() == "OLE2FRAME"]

    # ------------------------------------------------------------
    # Scale: compute a clean ratio that fits the model into the content
    # viewport, unless this is a semi-finished part (scale -> n/a). This
    # is plain, unconditional - the model's position/scale is never
    # nudged to "make room" for a BOM table or Notes (an earlier,
    # cleverer version tried that via viewport pan/zoom; per explicit
    # user feedback, this is simplified back down to two rules, see the
    # BOM/Notes overlap handling below: no auto-scaling, ever).
    # ------------------------------------------------------------
    vp = core.find_content_viewport(dst_layout)
    box = core.model_bbox(dst_doc)
    scale_ad = _find_scale_attdef(pairs)
    if vp is not None and box.has_data:
        fit_scale = core.compute_clean_scale(box.size.x, box.size.y, vp.dxf.width, vp.dxf.height)
        scale = core.ScaleResult("n/a", 1.0) if semi_finished else fit_scale
        core.apply_scale_to_viewport(vp, fit_scale, box.center)
        if scale_ad is not None:
            for a in new_insert.attribs:
                if a.dxf.tag == scale_ad.dxf.tag:
                    a.dxf.text = scale.ratio_text
    else:
        scale = core.ScaleResult("n/a", 1.0)
        warnings.append("找不到內容 viewport 或模型空間為空，比例欄位未自動計算，請手動確認。")

    # ------------------------------------------------------------
    # Overlap handling: title-block INSERTs typically draw the sheet's
    # own full-page border as part of the block (confirmed on the
    # Wellell family), so the insert's overall bbox is nearly the whole
    # page and useless for collision testing - almost anything near any
    # edge would "overlap" it, and the same is true of full-page
    # LINE/LWPOLYLINE border geometry among the imported furniture. Only
    # check against small, precise stand-ins for "content someone could
    # actually collide with": the title block's own filled-in ATTRIB
    # text, and A's imported table headers/cells (TEXT/MTEXT only - e.g.
    # the revision-table headers a B-side BOM table could genuinely land
    # on top of). Superseded near-exact duplicates were already handled
    # above (see `superseded`), which is the precise, low-false-positive
    # check; this is just a backstop for content that's genuinely new.
    # ------------------------------------------------------------
    # Row-index markers like a lone "0" are too short/generic to be a
    # meaningful collision target - MTEXT's box model is wide enough
    # that they falsely "overlap" an adjacent real data cell (e.g. B's
    # own "ALL" region value one column over). Only real table
    # headers/labels (more than 2 characters) are worth protecting here.
    # notes_entity is itself part of imported_furniture (A's Notes block
    # is reused/extended, not a fresh copy) - it must never be compared
    # against its own bbox, or a stale snapshot from before it was
    # combined/repositioned reads as "the Notes block overlaps itself".
    protected_entities = list(new_insert.attribs) + [
        e for e in imported_furniture
        if e.dxftype() in ("TEXT", "MTEXT") and len(_entity_text(e) or "") > 2
        and e is not notes_entity
    ]
    protected_boxes = [b for b in (_entity_bbox(pe) for pe in protected_entities) if b.has_data]

    # Any relocation must also stay outside every content viewport - we
    # can't see what it's displaying from model space, so treat the
    # whole viewport rectangle as off-limits rather than risk landing on
    # top of balloon leaders/circles.
    viewport_boxes = []
    for e in dst_layout:
        if e.dxftype() != "VIEWPORT":
            continue
        cx, cy = e.dxf.center.x, e.dxf.center.y
        hw, hh = e.dxf.width / 2, e.dxf.height / 2
        viewport_boxes.append(ezbbox.BoundingBox([Vec3(cx - hw, cy - hh, 0), Vec3(cx + hw, cy + hh, 0)]))

    # Anything relocated outside the frame this run gets tracked here so
    # later relocations check against it too - "不管是圖框內外的資訊都
    # 不能重疊": two things parked outside the frame independently (e.g.
    # BOM and Notes both anchored back to the same spot beside the sheet
    # border) must not just pile up on each other out there either.
    outside_boxes = []

    if ole_objects:
        # Simplified per explicit user request: no auto-resize, no
        # nearby-spot search, no viewport panning - just two checks. If
        # a BOM/OLE object overlaps the frame's own content or the
        # model view, move the WHOLE thing outside the frame (to the
        # right of the sheet) and leave it for manual placement;
        # otherwise leave it exactly where B had it.
        for ole in ole_objects:
            _relocate_outside_if_overlapping(
                ole, protected_boxes, viewport_boxes,
                dst_layout.dxf.paper_width, dst_layout.dxf.paper_height, warnings,
                outside_boxes=outside_boxes)
        if notes_entity is not None:
            _relocate_outside_if_overlapping(
                notes_entity, protected_boxes, viewport_boxes,
                dst_layout.dxf.paper_width, dst_layout.dxf.paper_height, warnings,
                label="備註 (Notes)", outside_boxes=outside_boxes)
    elif notes_entity is not None:
        # No BOM-like object in this drawing - keep the simpler,
        # previously-validated behaviour: move Notes up to clear the
        # border if needed, never shrinking the font.
        _fit_notes_in_frame(dst_layout, notes_entity, new_insert, warnings)

    frame_ids = {id(e) for e in [new_insert] + imported_furniture}
    skip_ids = frame_ids | {id(e) for e in ole_objects}
    if notes_entity is not None:
        skip_ids.add(id(notes_entity))
    others = [e for e in dst_layout if id(e) not in skip_ids and e.dxftype() != "VIEWPORT"]
    relocated = _relocate_if_overlapping(
        dst_doc, dst_layout, others, protected_entities, viewport_boxes,
        dst_layout.dxf.paper_width, dst_layout.dxf.paper_height, warnings,
        outside_boxes=outside_boxes)

    report = MergeReport(scale=scale, relocated_entities=relocated,
                          unmatched_attdefs=unmatched, warnings=warnings)
    return dst_doc, report
