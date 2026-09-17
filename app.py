"""
Wellell 圖框合併工具 (dwgmerge)
================================
Streamlit web app: merge legacy drawings ("B") into the company's
standard title-block template ("A"). See README.md for what this does
and does not do, and for deployment instructions.
"""
import io
import json
import tempfile
from datetime import datetime
from pathlib import Path

import ezdxf
import streamlit as st
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
import matplotlib.pyplot as plt

from dwgmerge_engine import core, merge
import github_store

st.set_page_config(page_title="Wellell 圖框合併工具", layout="wide")

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_DIR.mkdir(exist_ok=True)
DEFAULT_TEMPLATE_NAME = "current_template.dxf"

MODE_LABELS = {
    merge.MODE_B_CONTENT_A_STYLE: "模式 1：B 內容 + A 格式（目前驗證過的方法）",
    merge.MODE_B_STYLE_A_FRAME_ONLY: "模式 2：僅換 A 圖框，格式以 B 為主",
    merge.MODE_SIMPLE_COMBINE: "模式 3：單純結合，各自保留原格式",
}


# ---------------------------------------------------------------------
# Usage log: one row per file actually downloaded, so the「使用統計」分頁
# can show how many times this tool is actually being used - synced to
# GitHub when configured (shared/persistent across everyone), otherwise
# falls back to an in-session-only counter (see github_store.py).
# ---------------------------------------------------------------------

def log_download(filename: str):
    st.session_state["session_download_count"] = st.session_state.get("session_download_count", 0) + 1
    if github_store.is_usage_log_configured():
        try:
            github_store.append_usage_event(datetime.now().isoformat(timespec="seconds"), "download", filename)
        except Exception as e:
            st.warning(f"使用次數記錄失敗（不影響檔案下載）：{e}")


# ---------------------------------------------------------------------
# Template ("A") storage: local file, optionally synced to a GitHub repo
# so it persists across redeploys and everyone sees the same baseline.
# ---------------------------------------------------------------------

def load_template_path() -> Path:
    local_path = TEMPLATE_DIR / DEFAULT_TEMPLATE_NAME
    if github_store.is_configured():
        try:
            data = github_store.download_template()
            if data:
                local_path.write_bytes(data)
        except Exception as e:
            st.warning(f"無法從 GitHub 讀取共用基準圖框，使用本機暫存版本。({e})")
    return local_path


def save_template(uploaded_bytes: bytes, filename: str):
    local_path = TEMPLATE_DIR / DEFAULT_TEMPLATE_NAME
    local_path.write_bytes(uploaded_bytes)
    if github_store.is_configured():
        github_store.upload_template(uploaded_bytes, filename)
        st.success("已更新，並同步到 GitHub 共用儲存庫，之後所有使用者都會拿到這個新版本。")
    else:
        st.info("已更新本次執行環境的基準圖框（未設定 GitHub 同步，重新部署後會還原成內建版本）。")


def read_dxf_bytes(data: bytes) -> ezdxf.document.Drawing:
    """ezdxf needs a real file path (or a stream it fully controls) to
    read reliably - write the upload to a temp file rather than juggling
    text-mode stream wrappers around BytesIO, which are prone to being
    closed out from under us."""
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        f.write(data)
        path = f.name
    return ezdxf.readfile(path)


def write_dxf_bytes(doc: ezdxf.document.Drawing) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
        path = f.name
    doc.saveas(path)
    return Path(path).read_bytes()


def render_preview_png(doc, layout_name: str) -> bytes:
    lay = doc.layout(layout_name)
    fig = plt.figure(figsize=(11.7, 8.3))
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    out = MatplotlibBackend(ax)
    Frontend(ctx, out).draw_layout(lay, finalize=True)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


st.title("Wellell 圖框合併工具")
st.caption("把舊圖 (B) 合併進公司標準圖框 (A) — DXF only，AutoCAD 2000 (.dxf) 格式輸出")

tab_template, tab_merge, tab_stats, tab_about = st.tabs(
    ["① 基準圖框 (A)", "② 批次合併 (B)", "使用統計", "說明 / 限制"]
)

# =======================================================================
# TAB 1 — Template (A) management
# =======================================================================
with tab_template:
    st.subheader("目前的基準圖框 (A)")
    template_path = load_template_path()

    if template_path.exists():
        try:
            a_doc = ezdxf.readfile(str(template_path))
            layouts = core.list_layout_names(a_doc)
            st.write(f"目前基準圖框包含的圖紙 (Layout)：**{', '.join(layouts)}**")
        except Exception as e:
            st.error(f"目前的基準圖框讀取失敗：{e}")
    else:
        st.warning("尚未設定基準圖框，請上傳一份 DXF。")

    st.divider()
    st.subheader("更新基準圖框")
    st.caption(
        "上傳新的 A 圖框（DXF）。上傳後，之後所有使用者的合併作業都會採用新版本。"
        "圖框內至少要有一個含屬性 (ATTRIB/ATTDEF) 的標題欄 INSERT，"
        "以及你要提供的每個圖紙尺寸（如 A3 / A4 / A4-V）各自的 Layout。"
    )
    new_template_file = st.file_uploader("上傳新的基準圖框 (.dxf)", type=["dxf"], key="template_upload")
    if new_template_file is not None:
        if st.button("確認套用這份新的基準圖框", type="primary"):
            save_template(new_template_file.getvalue(), new_template_file.name)
            st.rerun()

# =======================================================================
# TAB 2 — Batch merge
# =======================================================================
with tab_merge:
    template_path = TEMPLATE_DIR / DEFAULT_TEMPLATE_NAME
    if not template_path.exists():
        st.warning("請先到「基準圖框 (A)」分頁設定基準圖框。")
    else:
        a_doc_preview = ezdxf.readfile(str(template_path))
        layout_names = core.list_layout_names(a_doc_preview)

        AUTO_LAYOUT_OPTION = "自動（依 B 模型尺寸選擇，建議）"
        AUTO_SEMI_OPTION = "自動判斷（依 B 圖是否含 BOM 表，建議）"
        SEMI_YES_OPTION = "手動：這是半成品 / 組裝件"
        SEMI_NO_OPTION = "手動：這不是半成品"

        st.subheader("設定")
        c1, c2, c3 = st.columns(3)
        with c1:
            target_layout_choice = st.selectbox(
                "要套用的圖紙尺寸 (A 的 Layout)", [AUTO_LAYOUT_OPTION] + layout_names
            )
        with c2:
            mode = st.selectbox(
                "結合模式", list(MODE_LABELS.keys()), format_func=lambda k: MODE_LABELS[k]
            )
        with c3:
            semi_finished_choice = st.selectbox(
                "半成品 / 組裝件判定（比例填 n/a，不需計算整數比例）",
                [AUTO_SEMI_OPTION, SEMI_YES_OPTION, SEMI_NO_OPTION],
            )

        with st.expander("額外要補充的 Notes 項目（選填 - B 舊圖自己的備註會自動比對合併，不需要在這裡重複輸入）"):
            notes_text = st.text_area(
                "每行一項。工具會自動讀取 B 舊圖的備註，跟 A 的備註逐項比對用詞："
                "類似的內容會自動去重（以 A 的用詞為準），B 真正新增的項目會接續編號加入；"
                "這裡輸入的是「B 圖檔裡沒有寫、你想額外加上去」的備註，會接在最後面繼續編號。",
                height=100,
            )

        st.divider()
        st.subheader("上傳要合併的舊圖 (B) — 可一次多個")
        b_files = st.file_uploader("上傳 B 圖面 (.dxf)，可複選", type=["dxf"], accept_multiple_files=True)

        output_formats = st.multiselect(
            "輸出格式", ["DXF (AutoCAD 2000)", "PNG 預覽圖"], default=["DXF (AutoCAD 2000)"]
        )

        # Results (log lines + the actual output files) are stashed in
        # session_state and re-rendered on EVERY run, not just the run
        # where "開始合併" was clicked - clicking a st.download_button
        # triggers its own rerun, on which "開始合併" reports unclicked
        # again; if the download buttons (and the data behind them)
        # only existed inside that first `if ... st.button(...)` block,
        # they'd vanish on the very next rerun and the click could never
        # be detected at all. Keeping them in session_state is what
        # makes "log every download" (see log_download()) actually work.
        if b_files and st.button("開始合併", type="primary"):
            extra_lines = [l.strip() for l in notes_text.splitlines() if l.strip()] or None
            today_mmdd = datetime.now().strftime("%m%d")
            log_entries = []   # list of ("write"|"error", text)
            outputs = []       # list of (filename, bytes, mime)
            images = []        # list of (filename, png_bytes, caption)
            for bf in b_files:
                log_entries.append(("write", f"### {bf.name}"))
                try:
                    a_doc = ezdxf.readfile(str(template_path))
                    b_doc = read_dxf_bytes(bf.getvalue())

                    if target_layout_choice == AUTO_LAYOUT_OPTION:
                        target_layout, layout_note = core.suggest_target_layout(a_doc, b_doc)
                        log_entries.append(("write", f"- 圖紙尺寸：{layout_note}"))
                    else:
                        target_layout = target_layout_choice

                    if semi_finished_choice == AUTO_SEMI_OPTION:
                        semi_finished = core.b_has_bom_objects(b_doc)
                        log_entries.append(("write", (
                            f"- 半成品判定：偵測到 B 圖{'含有' if semi_finished else '沒有'}"
                            f"BOM 表，自動判定為{'半成品/組裝件' if semi_finished else '一般零件圖'}。"
                        )))
                    else:
                        semi_finished = semi_finished_choice == SEMI_YES_OPTION

                    # AutoCAD 2000 == DXF version AC1015, output always
                    # normalised to this per the "統一轉AutoCAD 2000" rule.
                    out_doc, report = merge.run_merge(
                        a_doc, b_doc, target_layout, mode,
                        semi_finished=semi_finished, extra_note_lines=extra_lines,
                    )
                    out_doc.dxfversion = "AC1015"

                    # 輸出檔名：料號 版次(自動+1)_日期，例如
                    # "871203-0000 V1.1_0917" - 料號/版次來自 B 的檔名
                    # 跟標題欄版次 (版次衝突時以標題欄內容為準)。
                    b_attribs = core.b_titleblock_attribs(b_doc)
                    part_no, old_version, name_notes = core.resolve_part_and_version(
                        Path(bf.name).stem, b_attribs)
                    new_version = core.next_version(old_version)
                    out_stem = core.build_output_stem(part_no, new_version, today_mmdd)
                    for n in name_notes:
                        log_entries.append(("write", f"- 檔名：{n}"))

                    log_entries.append(("write", f"- 比例欄位：**{report.scale.ratio_text}**"))
                    if report.unmatched_attdefs:
                        log_entries.append(("write", f"- 找不到對應舊值、沿用 A 預設的欄位：{report.unmatched_attdefs}"))
                    if report.relocated_entities:
                        log_entries.append(("write", f"- 有 {report.relocated_entities} 個物件因為壓到標題欄，已移到圖框下方，請手動拖回。"))
                    for w in report.warnings:
                        log_entries.append(("write", f"- ⚠️ {w}"))

                    if "DXF (AutoCAD 2000)" in output_formats:
                        outputs.append((f"{out_stem}.dxf", write_dxf_bytes(out_doc), "application/dxf"))
                    if "PNG 預覽圖" in output_formats:
                        png = render_preview_png(out_doc, target_layout)
                        outputs.append((f"{out_stem}.png", png, "image/png"))
                        images.append((out_stem, png, f"{out_stem} 合併結果預覽"))
                except Exception as e:
                    log_entries.append(("error", f"{bf.name} 合併失敗：{e}"))

            st.session_state["merge_log_entries"] = log_entries
            st.session_state["merge_outputs"] = outputs
            st.session_state["merge_images"] = images

        for kind, text in st.session_state.get("merge_log_entries", []):
            (st.error if kind == "error" else st.write)(text)
        for stem, png, caption in st.session_state.get("merge_images", []):
            st.image(png, caption=caption, use_container_width=True)
        for i, (fname, data, mime) in enumerate(st.session_state.get("merge_outputs", [])):
            clicked = st.download_button(
                f"下載 {fname}", data=data, file_name=fname, mime=mime, key=f"dl_{i}")
            if clicked:
                log_download(fname)

# =======================================================================
# TAB 3 — Usage statistics
# =======================================================================
with tab_stats:
    st.subheader("使用統計")
    if github_store.is_usage_log_configured():
        try:
            log_bytes = github_store.download_usage_log()
        except Exception as e:
            st.error(f"讀取使用紀錄失敗：{e}")
            log_bytes = None
        stats = core.compute_usage_stats(log_bytes)
        c1, c2, c3 = st.columns(3)
        c1.metric("本月下載次數", stats["this_month"])
        c2.metric("今年下載次數", stats["this_year"])
        c3.metric("累計總下載次數", stats["total"])
        if stats["by_month"]:
            st.write("每月下載次數：")
            st.bar_chart(stats["by_month"])
        if log_bytes:
            st.download_button(
                "下載完整使用紀錄 (CSV)", data=log_bytes,
                file_name="dwgmerge_usage_log.csv", mime="text/csv",
            )
        else:
            st.caption("目前還沒有任何下載紀錄。")
    else:
        st.warning(
            "尚未設定 GitHub 同步（見 README「部署」章節的 `usage_log_path` 設定），"
            "使用次數無法跨使用者/跨重新部署累計，以下只是「這次瀏覽器分頁」暫時的計數，"
            "重新整理頁面或別人打開這個工具都不會算在一起。"
        )
        st.metric("這次瀏覽器分頁的下載次數", st.session_state.get("session_download_count", 0))

# =======================================================================
# TAB 4 — About / limitations
# =======================================================================
with tab_about:
    st.markdown(Path(__file__).with_name("ABOUT.md").read_text(encoding="utf-8"))
