#!/usr/bin/env python3
"""Generate the hero's flanking spy figure in halftone-POSITION style:
the dot layout of a printed halftone (tight square grid, coverage decides
which cells get a mark, edges thin out), but every mark is a uniform '#'
so the figure reads dense.

Outputs:
  - text art to stdout (paste into the .ascii <pre>s; square pitch comes
    from CSS: line-height == char advance)
  - web/agent-halftone.svg is ALSO refreshed with the same dot population
    (round-dot reference version, kept for comparison/reuse)

Silhouette: fedora, sunglasses band, bust with V notch (classic spy icon).
"""
import math, random
from pathlib import Path

GRID_W, GRID_H = 56, 76   # H crops the bust flat at the bottom
PITCH = 10
R_MAX = 0.40 * PITCH      # clear gaps between dots (less clustered)
CX = GRID_W / 2.0
rng = random.Random(11)

def hat_dome(x, Y):
    return ((x-CX)/9.0)**2 + ((Y-13)/8.5)**2 <= 1 and Y <= 15

def hat_crown(x, Y):
    if not (13 <= Y <= 24): return False
    return abs(x-CX) <= 9.0 + (Y-13)*0.16

def hat_brim(x, Y):
    return ((x-CX)/17.5)**2 + ((Y-26.5)/3.6)**2 <= 1

def glasses(x, Y):
    for ex in (CX-6.3, CX+6.3):
        if ((x-ex)/5.6)**2 + ((Y-37.5)/4.0)**2 <= 1: return True
    if abs(x-CX) <= 2.3 and 35 <= Y <= 37.5: return True
    if 34.5 <= Y <= 37 and (abs(x-(CX-13.2)) <= 1.6 or abs(x-(CX+13.2)) <= 1.6):
        return True
    return False

def bust(x, Y):
    if Y < 49: return False
    if ((x-CX)/19.5)**2 + ((Y-74)/24.5)**2 > 1: return False
    if Y <= 71 and abs(x-CX) < (71-Y)*0.30: return False
    return True

def inside(x, Y):
    return hat_dome(x,Y) or hat_crown(x,Y) or hat_brim(x,Y) or glasses(x,Y) or bust(x,Y)

circles, text_rows = [], []
for row in range(GRID_H):
    line = []
    for col in range(GRID_W):
        hits = 0
        for dy in (-0.33, 0.0, 0.33):
            for dx in (-0.33, 0.0, 0.33):
                if inside(col+0.5+dx, row+0.5+dy): hits += 1
        cov = hits / 9.0
        if cov <= 0:
            line.append(' '); continue
        cov = min(1.0, cov * (0.88 + 0.24*rng.random()))
        r = R_MAX * math.sqrt(cov)
        if r < 1.0:                      # same population cutoff as the dot version
            line.append(' '); continue
        x = col*PITCH + PITCH//2
        y = row*PITCH + PITCH//2
        circles.append(f'<circle cx="{x}" cy="{y}" r="{r:.1f}"/>')
        line.append('#')                 # uniform mark at the halftone dot position
    text_rows.append(''.join(line).rstrip())

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
       f'viewBox="0 0 {GRID_W*PITCH} {GRID_H*PITCH}" fill="#141414">\n'
       + "\n".join(circles) + "\n</svg>\n")
(Path(__file__).resolve().parent / "agent-halftone.svg").write_text(svg)

lines = text_rows[:]
while lines and not lines[0].strip(): lines.pop(0)
while lines and not lines[-1].strip(): lines.pop()
print('\n'.join(lines))
