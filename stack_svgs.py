#!/usr/bin/env python3
"""
stack_svgs.py — pack SVG label files onto an A4 sheet.

Outputs two files in the current directory:
  stacked.svg  — vector SVG (editable in Inkscape)
  stacked.pdf  — print-ready A4 PDF, fully vector (no rasterisation)

Requirements: cairosvg, pypdf  (pip install cairosvg pypdf)
Each source SVG must have width="Xmm" height="Ymm" attributes (Inkscape default).
"""

import os
import re
import tempfile
import xml.etree.ElementTree as ET

import cairosvg
from pypdf import PdfWriter, PdfReader, Transformation

# ── layout parameters ─────────────────────────────────────────────────────────
A4_W_MM   = 210
A4_H_MM   = 297
MARGIN_MM = 10
COL_GAP   = 1.0   # horizontal gap between labels [mm]
ROW_GAP   = 1.0   # vertical gap between rows     [mm]
PT_PER_MM = 72 / 25.4
# ─────────────────────────────────────────────────────────────────────────────

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("",         SVG_NS)
ET.register_namespace("xlink",    "http://www.w3.org/1999/xlink")
ET.register_namespace("dc",       "http://purl.org/dc/elements/1.1/")
ET.register_namespace("cc",       "http://creativecommons.org/ns#")
ET.register_namespace("rdf",      "http://www.w3.org/1999/02/22-rdf-syntax-ns#")
ET.register_namespace("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd")
ET.register_namespace("inkscape", "http://www.inkscape.org/namespaces/inkscape")


# ── SVG parsing ───────────────────────────────────────────────────────────────

def parse_mm_attr(value, attr_name, path):
    m = re.match(r"^\s*([\d.]+)\s*mm\s*$", value or "")
    if not m:
        raise ValueError(f"{path}: {attr_name!r} not in mm, got {value!r}")
    return float(m.group(1))

def strip_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag

def parse_svg(path):
    """Return (w_mm, h_mm, viewbox_tuple, root_element, raw_bytes)."""
    with open(path, "rb") as f:
        raw = f.read()
    tree = ET.parse(path)
    root = tree.getroot()
    assert strip_ns(root.tag) == "svg", f"{path}: root is not <svg>"
    w_mm = parse_mm_attr(root.get("width",  ""), "width",  path)
    h_mm = parse_mm_attr(root.get("height", ""), "height", path)
    vb = root.get("viewBox")
    if vb:
        vb_tuple = tuple(float(p) for p in re.split(r"[\s,]+", vb.strip()))
    else:
        vb_tuple = (0.0, 0.0, w_mm, h_mm)
    print(f"  {os.path.basename(path):40s}  {w_mm:.2f} × {h_mm:.2f} mm")
    return w_mm, h_mm, vb_tuple, root, raw


# ── packing ───────────────────────────────────────────────────────────────────

def pack(items, max_w_mm, col_gap, row_gap):
    """Row-wrap with per-row horizontal centering."""
    rows, current_row, x = [], [], 0.0
    for item in items:
        w_mm = item[1]
        if x > 0 and x + w_mm > max_w_mm:
            rows.append(current_row)
            current_row, x = [], 0.0
        current_row.append((item, x))
        x += w_mm + col_gap
    if current_row:
        rows.append(current_row)

    placements, y = [], 0.0
    for row in rows:
        last_item, last_x = row[-1]
        row_w    = last_x + last_item[1]
        x_offset = (max_w_mm - row_w) / 2.0
        row_h    = max(item[2] for item, _ in row)
        for item, x_in_row in row:
            placements.append((x_in_row + x_offset, y, item))
        y += row_h + row_gap

    return placements, y - row_gap


# ── SVG output ────────────────────────────────────────────────────────────────

def write_svg(placements, content_h_mm, out_path):
    total_w = A4_W_MM
    total_h = content_h_mm + 2 * MARGIN_MM

    out = ET.Element("svg", {
        "xmlns":       SVG_NS,
        "xmlns:xlink": "http://www.w3.org/1999/xlink",
        "width":       f"{total_w}mm",
        "height":      f"{total_h:.4f}mm",
        "viewBox":     f"0 0 {total_w} {total_h:.4f}",
    })
    ET.SubElement(out, "rect", {
        "x": "0", "y": "0",
        "width": str(total_w), "height": f"{total_h:.4f}",
        "fill": "#ffffff",
    })
    for x_mm, y_mm, item in placements:
        _name, w_mm, h_mm, vb_tuple, src_root, _raw = item
        tx, ty = MARGIN_MM + x_mm, MARGIN_MM + y_mm
        vb_x, vb_y, vb_w, vb_h = vb_tuple
        wrapper = ET.SubElement(out, "svg", {
            "x": f"{tx:.6f}", "y": f"{ty:.6f}",
            "width": f"{w_mm:.6f}", "height": f"{h_mm:.6f}",
            "viewBox": f"{vb_x} {vb_y} {vb_w} {vb_h}",
            "preserveAspectRatio": "xMinYMin meet",
        })
        for child in src_root:
            wrapper.append(child)

    try:
        ET.indent(out, space="  ")
    except AttributeError:
        pass
    ET.ElementTree(out).write(out_path, encoding="unicode", xml_declaration=False)
    print(f"SVG → {out_path}")


# ── PDF output (fully vector) ─────────────────────────────────────────────────

def write_pdf(placements, out_path):
    """
    Each source SVG is converted to a single-page PDF by cairosvg (vector).
    pypdf then merges each label page onto an A4 blank using a translate
    transformation.  No rasterisation at any stage.

    Coordinate systems:
      SVG / screen  — origin top-left,  Y increases downward
      PDF / ReportLab — origin bottom-left, Y increases upward
    Conversion:  y_pdf = page_h_pt - y_top_pt - label_h_pt
    """
    a4_w_pt = A4_W_MM * PT_PER_MM
    a4_h_pt = A4_H_MM * PT_PER_MM

    writer = PdfWriter()
    page = writer.add_blank_page(width=a4_w_pt, height=a4_h_pt)

    with tempfile.TemporaryDirectory() as tmp:
        for idx, (x_mm, y_mm, item) in enumerate(placements):
            _name, w_mm, h_mm, _vb, _root, raw_svg = item

            # cairosvg: SVG → single-page vector PDF in memory
            pdf_bytes = cairosvg.svg2pdf(bytestring=raw_svg)

            tmp_pdf = os.path.join(tmp, f"label_{idx:03d}.pdf")
            with open(tmp_pdf, "wb") as f:
                f.write(pdf_bytes)

            label_page = PdfReader(tmp_pdf).pages[0]

            # Position: top-left corner of label in pt
            x_pt     = (MARGIN_MM + x_mm) * PT_PER_MM
            y_top_pt = (MARGIN_MM + y_mm) * PT_PER_MM
            # PDF Y origin is at bottom — flip
            y_pt = a4_h_pt - y_top_pt - (h_mm * PT_PER_MM)

            page.merge_transformed_page(
                label_page,
                Transformation().translate(x_pt, y_pt),
            )

    with open(out_path, "wb") as f:
        writer.write(f)
    print(f"PDF → {out_path}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    folder = os.getcwd()
    files  = sorted(
        (f for f in os.listdir(folder)
         if f.lower().endswith(".svg") and f != "stacked.svg"),
        key=lambda f: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', f)],
    )

    if not files:
        print("No SVG files found in", folder)
        return

    print(f"Found {len(files)} SVG(s) in {folder}\n")

    items = []
    for f in files:
        try:
            items.append((f, *parse_svg(os.path.join(folder, f))))
        except Exception as e:
            print(f"  SKIP {f}: {e}")

    if not items:
        print("No usable SVGs.")
        return

    usable_w = A4_W_MM - 2 * MARGIN_MM
    placements, content_h = pack(items, usable_w, COL_GAP, ROW_GAP)
    print(f"\n{len(placements)} label(s), content height {content_h:.2f} mm\n")

    write_svg(placements, content_h, os.path.join(folder, "stacked.svg"))
    write_pdf(placements,            os.path.join(folder, "stacked.pdf"))


if __name__ == "__main__":
    main()
