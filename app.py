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
import zipfile
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

tab_template, tab_merge, tab_about = st.tabs(["① 基準圖框 (A)", "② 批次合併 (B)", "說明 / 限制"])

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
            cols = st.columns(min(3, len(layouts)) or 1)
            for i, lname in enumerate(layouts):
                with cols[i % len(cols)]:
                    st.write(f"**{lname}**")
                    try:
                        png = render_preview_png(a_doc, lname)
                        st.image(png, use_container_width=True)
                    except Exception as e:
                        st.write(f"(預覽失敗: {e})")
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

        st.subheader("設定")
        c1, c2, c3 = st.columns(3)
        with c1:
            target_layout = st.selectbox("要套用的圖紙尺寸 (A 的 Layout)", layout_names)
        with c2:
            mode = st.selectbox(
                "結合模式", list(MODE_LABELS.keys()), format_func=lambda k: MODE_LABELS[k]
            )
        with c3:
            semi_finished = st.checkbox("這是半成品 / 組裝件（比例填 n/a，不需計算整數比例）")

        with st.expander("要在 Notes 補充的項目（接續在 A 的備註後面編號）"):
            notes_text = st.text_area(
                "每行一項，如果 A 的備註已有類似內容，工具不會自動判斷語意重複，"
                "請自行確認要不要保留這行。",
                height=100,
            )

        st.divider()
        st.subheader("上傳要合併的舊圖 (B) — 可一次多個")
        b_files = st.file_uploader("上傳 B 圖面 (.dxf)，可複選", type=["dxf"], accept_multiple_files=True)

        output_formats = st.multiselect(
            "輸出格式", ["DXF (AutoCAD 2000)", "PNG 預覽圖"], default=["DXF (AutoCAD 2000)", "PNG 預覽圖"]
        )

        if b_files and st.button("開始合併", type="primary"):
            extra_lines = [l.strip() for l in notes_text.splitlines() if l.strip()] or None
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for bf in b_files:
                    st.write(f"### {bf.name}")
                    try:
                        a_doc = ezdxf.readfile(str(template_path))
                        b_doc = read_dxf_bytes(bf.getvalue())
                        # AutoCAD 2000 == DXF version AC1015, output always
                        # normalised to this per the "統一轉AutoCAD 2000" rule.
                        out_doc, report = merge.run_merge(
                            a_doc, b_doc, target_layout, mode,
                            semi_finished=semi_finished, extra_note_lines=extra_lines,
                        )
                        out_doc.dxfversion = "AC1015"

                        st.write(f"- 比例欄位：**{report.scale.ratio_text}**")
                        if report.unmatched_attdefs:
                            st.write(f"- 找不到對應舊值、沿用 A 預設的欄位：{report.unmatched_attdefs}")
                        if report.relocated_entities:
                            st.write(f"- 有 {report.relocated_entities} 個物件因為壓到標題欄，已移到圖框下方，請手動拖回。")
                        for w in report.warnings:
                            st.write(f"- ⚠️ {w}")

                        stem = Path(bf.name).stem
                        if "DXF (AutoCAD 2000)" in output_formats:
                            zf.writestr(f"{stem}_merged.dxf", write_dxf_bytes(out_doc))
                        if "PNG 預覽圖" in output_formats:
                            png = render_preview_png(out_doc, target_layout)
                            zf.writestr(f"{stem}_merged_preview.png", png)
                            st.image(png, caption=f"{stem} 合併結果預覽", use_container_width=True)
                    except Exception as e:
                        st.error(f"{bf.name} 合併失敗：{e}")

            zip_buffer.seek(0)
            st.download_button(
                "下載全部結果 (ZIP)", data=zip_buffer, file_name="merged_drawings.zip",
                mime="application/zip",
            )

# =======================================================================
# TAB 3 — About / limitations
# =======================================================================
with tab_about:
    st.markdown(Path(__file__).with_name("ABOUT.md").read_text(encoding="utf-8"))
