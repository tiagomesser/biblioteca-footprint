# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""KiCad Custom Design Rules (.kicad_dru) aus den Ergebnissen schreiben.

Die Netzklassen-Werte steuern den Router; die .kicad_dru sichert die Werte
zusaetzlich per DRC ab. Genutzte Constraints der KiCad-Regelsprache:
track_width und diff_pair_gap (die Bahnbreite eines Paars ist track_width).
"""
from __future__ import annotations

from typing import Dict


def _fmt(nm: int) -> str:
    s = f"{nm / 1e6:.4f}".rstrip("0").rstrip(".")
    return f"{s}mm"


def build_dru(changes: Dict[str, Dict[str, int]], tol_pct: float = 10.0,
              layer_rules=None) -> str:
    """changes: {Netzklasse: {Feld: nm}}; Felder: track_width, diff_pair_gap.
    layer_rules: optional [(Netzklasse, Lage, {Feld: nm})] - lagenbezogene
    Regeln mit (layer ...)-Klausel, damit jede Signallage ihren eigenen
    Sollwert bekommt. Rueckgabe: Dateiinhalt fuer <board>.kicad_dru."""
    lines = ["(version 1)",
             "# Erzeugt vom Z0 Field Solver Plugin.",
             "# Toleranz: +/-{:.0f}% um den Sollwert.".format(tol_pct)]
    f = 1.0 - tol_pct / 100.0
    g = 1.0 + tol_pct / 100.0
    for cls, fields in changes.items():
        cond = f'(condition "A.NetClass == \'{cls}\'")'
        cons = []
        nm = fields.get("track_width")
        if nm:
            cons.append(
                f'(constraint track_width (min {_fmt(int(nm*f))}) (opt {_fmt(nm)}) (max {_fmt(int(nm*g))}))'
            )
        dpg = fields.get("diff_pair_gap")
        if dpg:
            cons.append(
                f'(constraint diff_pair_gap (min {_fmt(int(dpg*f))}) (opt {_fmt(dpg)}) (max {_fmt(int(dpg*g))}))'
            )
        if cons:
            lines.append(f'(rule "Impedanz {cls}" {cond} ' + " ".join(cons) + ")")
    for cls, layer, fields in layer_rules or []:
        cond = f'(condition "A.NetClass == \'{cls}\'")'
        lay = f'(layer "{layer}")'
        cons = []
        nm = fields.get("track_width")
        if nm:
            cons.append(
                f'(constraint track_width (min {_fmt(int(nm*f))}) (opt {_fmt(nm)}) (max {_fmt(int(nm*g))}))'
            )
        dpg = fields.get("diff_pair_gap")
        if dpg:
            cons.append(
                f'(constraint diff_pair_gap (min {_fmt(int(dpg*f))}) (opt {_fmt(dpg)}) (max {_fmt(int(dpg*g))}))'
            )
        if cons:
            lines.append(f'(rule "Impedanz {cls} auf {layer}" {lay} {cond} '
                         + " ".join(cons) + ")")
    return "\n".join(lines) + "\n"
