#!/usr/bin/env python3
"""
SVG Label Generator — multi-label, multi-line support.
Each CSV row defines one label layout.
Each cell may contain multi-line text separated by '\n'.

Requirements:
    pip install svgwrite fonttools
"""

import csv
import re
import svgwrite
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen
import subprocess

# ─────────────────────────────────────────────
#  Glyph recorder
# ─────────────────────────────────────────────

class GlyphRecorder:
    def __init__(self, font_path):
        font       = TTFont(font_path)
        self._gs   = font.getGlyphSet()
        self._cmap = font.getBestCmap()
        self._hmtx = font["hmtx"].metrics
        self._upm  = font["head"].unitsPerEm
        os2        = font["OS/2"]
        self.cap_height = os2.sCapHeight if os2.sCapHeight else int(self._upm * 0.7)

    def _name(self, ch):
        return self._cmap.get(ord(ch))

    def record_multiline(self, text, line_gap_ratio):
        """Records multi-line text as one object, centered horizontally."""
        lines = text.split("\n")
        gap   = self.cap_height * line_gap_ratio

        raw_lines, widths = [], []
        for line in lines:
            x_cursor = 0
            ops = []
            for ch in line:
                name = self._name(ch)
                if not name:
                    x_cursor += self._upm // 4
                    continue
                pen = RecordingPen()
                self._gs[name].draw(pen)
                for op, args in pen.value:
                    if op == "moveTo":
                        x, y = args[0]
                        ops.append(("M", [(x + x_cursor, y)]))
                    elif op == "lineTo":
                        x, y = args[0]
                        ops.append(("L", [(x + x_cursor, y)]))
                    elif op == "curveTo":
                        ops.append(("C", [(x + x_cursor, y) for x, y in args]))
                    elif op == "qCurveTo":
                        ops.append(("Q", [(x + x_cursor, y) for x, y in args]))
                    elif op in ("closePath", "endPath"):
                        ops.append(("Z", []))
                x_cursor += self._hmtx[name][0]
            raw_lines.append(ops)
            widths.append(x_cursor)

        if not raw_lines:
            return []

        max_w = max(widths)
        ops_final, y_cursor = [], 0

        for ops, w in zip(raw_lines, widths):
            if not ops:
                continue

            dx = (max_w - w) / 2

            ys = [y for _, pts in ops for x, y in pts]
            line_y0, line_y1 = min(ys), max(ys)
            line_h = line_y1 - line_y0

            shift_y = y_cursor - line_y1

            for op, pts in ops:
                ops_final.append((op, [(x + dx, y + shift_y) for x, y in pts]))

            y_cursor = y_cursor - line_h - gap

        return ops_final

    def bbox(self, ops):
        xs = [x for _, pts in ops for x, y in pts]
        ys = [y for _, pts in ops for x, y in pts]
        return min(xs), min(ys), max(xs), max(ys)


# ─────────────────────────────────────────────
#  Path builder
# ─────────────────────────────────────────────

def ops_to_path_d(ops, tx, ty):
    parts = []
    for op, pts in ops:
        if op == "M":
            x, y = pts[0]
            parts.append(f"M{tx(x)},{ty(y)}")
        elif op == "L":
            x, y = pts[0]
            parts.append(f"L{tx(x)},{ty(y)}")
        elif op == "C":
            for i in range(0, len(pts), 3):
                x1,y1 = pts[i]; x2,y2 = pts[i+1]; x,y = pts[i+2]
                parts.append(f"C{tx(x1)},{ty(y1)} {tx(x2)},{ty(y2)} {tx(x)},{ty(y)}")
        elif op == "Q":
            on_curve   = pts[-1]
            off_curves = pts[:-1]
            for i, off in enumerate(off_curves):
                if i < len(off_curves) - 1:
                    nxt     = off_curves[i + 1]
                    implied = ((off[0]+nxt[0])/2, (off[1]+nxt[1])/2)
                    parts.append(f"Q{tx(off[0])},{ty(off[1])} {tx(implied[0])},{ty(implied[1])}")
                else:
                    parts.append(f"Q{tx(off[0])},{ty(off[1])} {tx(on_curve[0])},{ty(on_curve[1])}")
        elif op == "Z":
            parts.append("Z")
    return " ".join(parts)

def r(v): return round(v, 5)


# ─────────────────────────────────────────────
#  SVG builder
# ─────────────────────────────────────────────

def build_svg(cfg):
    rows, cols = cfg["rows"], cfg["columns"]
    w, h = cfg["width_mm"], cfg["height_mm"]
    border = cfg.get("canvas_thickness_mm", 1)
    clearance = cfg["text_clearance_mm"]
    stroke_w = cfg["stroke_width_mm"]
    gap_ratio = cfg["line_gap_ratio"]

    inner_w, inner_h = w - border * 2, h - border * 2
    cell_w, cell_h = inner_w / cols, inner_h / rows
    margin = stroke_w + clearance

    recorder = GlyphRecorder(cfg["font_path"])
    dwg = svgwrite.Drawing(cfg["output_file"], size=(f"{w}mm", f"{h}mm"), profile="full")
    dwg.attribs["viewBox"] = f"0 0 {w} {h}"

    dwg.add(dwg.rect(insert=(0, 0), size=(w, h), fill=cfg.get("canvas_color", "#000000")))
    dwg.add(dwg.rect(insert=(border, border), size=(inner_w, inner_h),
                     fill=cfg.get("background_color", "#90EE90")))

    texts = cfg["texts"]

    for row in range(rows):
        for col in range(cols):
            idx = row * cols + col
            if idx >= len(texts):
                continue

            text_val = texts[idx]
            ops = recorder.record_multiline(text_val, gap_ratio)
            if not ops:
                continue

            x0, y0, x1, y1 = recorder.bbox(ops)
            ink_w, ink_h = x1 - x0, y1 - y0

            scale = min(
                (cell_w - 2 * margin) / ink_w if ink_w else 1e9,
                (cell_h - 2 * margin) / ink_h if ink_h else 1e9,
            )

            cx = border + col * cell_w + cell_w / 2
            cy = border + row * cell_h + cell_h / 2
            ink_cx, ink_cy = (x0 + x1) / 2, (y0 + y1) / 2

            def tx(fx, _cx=cx, _icx=ink_cx, _s=scale): return r(_cx + (fx - _icx) * _s)
            def ty(fy, _cy=cy, _icy=ink_cy, _s=scale): return r(_cy - (fy - _icy) * _s)

            path_d = ops_to_path_d(ops, tx, ty)

            dwg.add(dwg.path(d=path_d, fill="none",
                             stroke=cfg["stroke_color"],
                             stroke_width=stroke_w * 2,
                             stroke_linejoin="round",
                             stroke_linecap="round"))

            dwg.add(dwg.path(d=path_d, fill=cfg["font_color"], stroke="none"))

    dwg.save()
    print(f"Saved → {cfg['output_file']}  ({w}×{h} mm)")


# ─────────────────────────────────────────────
#  Color resolver
# ─────────────────────────────────────────────

_COLOR_TABLE = None

def _load_color_table():
    global _COLOR_TABLE
    if _COLOR_TABLE is not None:
        return _COLOR_TABLE
    _COLOR_TABLE = {}
    try:
        with open("colors.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                _COLOR_TABLE[row["name"].strip().lower()] = row["hex"].strip()
    except FileNotFoundError:
        pass  # no table — only hex values will work
    return _COLOR_TABLE

def resolve_color(value, field_name="color"):
    """Accept a hex color (#RGB / #RRGGBB) or a name from colors.csv."""
    v = value.strip()
    if re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", v):
        return v
    table = _load_color_table()
    key = v.lower()
    if key in table:
        return table[key]
    raise ValueError(
        f"{field_name}: {v!r} is not a valid hex color and not found in colors.csv"
    )


# ─────────────────────────────────────────────
#  CSV loader
# ─────────────────────────────────────────────

def load_configs_from_csv(path):
    configs = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cfg = {
                "width_mm": float(row["width_mm"]),
                "height_mm": float(row["height_mm"]),
                "rows": int(row["rows"]),
                "columns": int(row["columns"]),
                "texts": [t.replace("\\n", "\n") for t in row["texts"].split("|")],
                "font_path": row["font_path"],
                "text_clearance_mm": float(row["text_clearance_mm"]),
                "font_color":        resolve_color(row["font_color"],        "font_color"),
                "stroke_width_mm": float(row["stroke_width_mm"]),
                "stroke_color":      resolve_color(row["stroke_color"],      "stroke_color"),
                "line_gap_ratio": float(row["line_gap_ratio"]),
                "canvas_color":      resolve_color(row.get("canvas_color",      "#000000"), "canvas_color"),
                "background_color":  resolve_color(row.get("background_color",  "#90EE90"), "background_color"),
            }
            configs.append(cfg)
    return configs


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 LabelGenerator.py labels.csv")
        sys.exit(1)

    csv_path = sys.argv[1]
    configs = load_configs_from_csv(csv_path)

    for i, cfg in enumerate(configs):
        cfg["output_file"] = f"label_{i+1}.svg"
        build_svg(cfg)
        
    subprocess.run(["python3", "stack_svgs.py"], check=True)