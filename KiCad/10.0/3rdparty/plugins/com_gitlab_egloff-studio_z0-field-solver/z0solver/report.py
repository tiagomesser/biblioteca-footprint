# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Ergebnisbericht als Text/HTML - auch Fallback, wenn wx nicht verfuegbar ist."""
from __future__ import annotations

import html
import time
from typing import Dict, List, Optional

from layermodel import LayerModel

MM = 1e-3


def result_lines(results: List[dict]) -> List[str]:
    lines = []
    for r in results:
        cls = r.get("netclass", "?")
        if r.get("error"):
            lines.append(f"{cls}: FEHLER - {r['error']}")
            continue
        if r.get("z0_result") is not None:
            res = r["z0_result"]
            lines.append(
                f"{cls}: Z0-Ziel {r['z0_target']:g} Ohm auf {r['layer']} -> "
                f"Breite {r['track_width_mm']:.4f} mm (gerechnet: {res.z0:.2f} Ohm, "
                f"eps_eff {res.eps_eff:.2f})"
            )
        if r.get("zdiff_result") is not None:
            res = r["zdiff_result"]
            lines.append(
                f"{cls}: Zdiff-Ziel {r['zdiff_target']:g} Ohm auf {r['layer']} -> "
                f"Breite {r['dp_width_mm']:.4f} mm, Gap {r['dp_gap_mm']:.4f} mm "
                f"(gerechnet: {res.zdiff:.2f} Ohm)"
            )
    return lines


def build_html(title: str, stackup_lines: List[str], models: Dict[str, LayerModel],
               results: List[dict], notes: Optional[List[str]] = None) -> str:
    def esc(s):
        return html.escape(str(s))

    parts = [f"<html><head><meta charset='utf-8'><title>{esc(title)}</title></head><body>",
             f"<h1>{esc(title)}</h1>",
             f"<p>Erzeugt {time.strftime('%Y-%m-%d %H:%M:%S')} vom Z0 Field Solver Plugin.</p>",
             "<h2>Lagenaufbau</h2><ul>"]
    parts += [f"<li>{esc(l)}</li>" for l in stackup_lines]
    parts.append("</ul><h2>Rechenmodelle</h2><ul>")
    for lid, m in models.items():
        if m.kind == "microstrip":
            parts.append(
                f"<li>{esc(lid)}: Microstrip, h={m.h_mm:.4f} mm, eps_r={m.er1:.2f}, "
                f"t={m.t_mm*1000:.0f} um, Bezug {esc(m.ref_below or '-')}"
                + (f", Lack {m.mask_t_mm*1000:.0f} um" if m.mask_t_mm else "")
                + (f" - Probleme: {esc('; '.join(m.problems))}" if m.problems else "")
                + "</li>")
        else:
            parts.append(
                f"<li>{esc(lid)}: Stripline, h={m.h_mm:.4f}/{m.h2_mm:.4f} mm, "
                f"eps_r={m.er1:.2f}/{m.er2:.2f}, t={m.t_mm*1000:.0f} um, "
                f"Bezug {esc(m.ref_above or '-')}/{esc(m.ref_below or '-')}"
                + (f" - Probleme: {esc('; '.join(m.problems))}" if m.problems else "")
                + "</li>")
    parts.append("</ul><h2>Ergebnisse</h2><ul>")
    parts += [f"<li>{esc(l)}</li>" for l in result_lines(results)]
    parts.append("</ul>")
    if notes:
        parts.append("<h2>Hinweise</h2><ul>")
        parts += [f"<li>{esc(n)}</li>" for n in notes]
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)
