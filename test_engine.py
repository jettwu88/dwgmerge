"""Sanity test: run the generalized engine on the same A/B pair we
validated manually earlier in this project, and check the results make
sense (no crash, scale is a clean ratio, key fields carried over)."""
import sys
sys.path.insert(0, ".")

import ezdxf
from dwgmerge_engine import merge, core

A_PATH = "templates/Wellell-standard-V7-2004.dxf"
B_PATH = "../work/871203-0000_V1.0.dxf"

a_doc = ezdxf.readfile(A_PATH)
b_doc = ezdxf.readfile(B_PATH)

print("A layouts:", core.list_layout_names(a_doc))

for mode_name, mode in [("MODE1", merge.MODE_B_CONTENT_A_STYLE),
                         ("MODE2", merge.MODE_B_STYLE_A_FRAME_ONLY),
                         ("MODE3", merge.MODE_SIMPLE_COMBINE)]:
    a_doc = ezdxf.readfile(A_PATH)
    b_doc = ezdxf.readfile(B_PATH)
    out_doc, report = merge.run_merge(
        a_doc, b_doc, target_layout_name="A3", mode=mode,
        semi_finished=False,
        extra_note_lines=[
            "快速接頭與PVC管組立前，須在PVC管內緣塗膠再組立。",
            "使用Satlon D-3或JS-20快乾膠。",
        ] if mode == merge.MODE_B_CONTENT_A_STYLE else None,
    )
    out_path = f"test_out_{mode_name}.dxf"
    out_doc.saveas(out_path)
    print(f"--- {mode_name} ---")
    print("scale:", report.scale)
    print("relocated:", report.relocated_entities)
    print("unmatched attdefs:", report.unmatched_attdefs)
    print("warnings:", report.warnings)
    print("saved:", out_path)
