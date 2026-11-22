# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Gebaute Bahnen gegen die gerechneten Zielbreiten pruefen (nur Breite je Lage).

Reine Logik (testbar ohne KiCad): check_tracks(items, targets).
Live-Adapter: collect_tracks(board) liest Bahnen und Netzklassen ueber die IPC API.
Differenz-Gap wird NICHT geprueft (die API liefert keine Paarzuordnung);
dafuer gibt es die .kicad_dru-Regeln im KiCad-DRC.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def check_tracks(items: List[Tuple[str, str, str, int]],
                 targets: Dict[Tuple[str, str], Tuple[int, float]]) -> List[dict]:
    """items: (net, netclass, layer, width_nm). targets[(netclass, layer)] = (width_nm, tol_frac).
    Rueckgabe: je Klasse+Lage Anzahl, Abweichler, Min/Max-Breite."""
    agg: Dict[Tuple[str, str], dict] = {}
    for net, cls, layer, w in items:
        tgt = targets.get((cls, layer))
        if tgt is None:
            # Klasse hat ein Ziel, aber nicht auf dieser Lage -> Hinweis
            if any(k[0] == cls for k in targets):
                a = agg.setdefault((cls, layer), {"cls": cls, "layer": layer, "n": 0, "bad": 0,
                                                  "target": None, "wmin": w, "wmax": w, "nets": set(),
                                                  "off_layer": True})
                a["n"] += 1; a["bad"] += 1; a["nets"].add(net)
                a["wmin"] = min(a["wmin"], w); a["wmax"] = max(a["wmax"], w)
            continue
        t, tol = tgt
        a = agg.setdefault((cls, layer), {"cls": cls, "layer": layer, "n": 0, "bad": 0, "target": t,
                                          "wmin": w, "wmax": w, "nets": set(), "off_layer": False})
        a["n"] += 1
        if abs(w - t) > tol * t:
            a["bad"] += 1
            a["nets"].add(net)
        a["wmin"] = min(a["wmin"], w); a["wmax"] = max(a["wmax"], w)
    return sorted(agg.values(), key=lambda r: (r["cls"], r["layer"]))


def collect_tracks(board: Any) -> List[Tuple[str, str, str, int]]:
    from stackup import kicad_layer_name
    tracks = [t for t in board.get_tracks() if hasattr(t, "width")]
    nets = {}
    for t in tracks:
        nets[t.net.name] = t.net
    cls_map = board.get_netclass_for_nets(list(nets.values())) if nets else {}
    out = []
    for t in tracks:
        nc = cls_map.get(t.net.name)
        cname = getattr(nc, "name", None) or "Default"
        out.append((t.net.name, cname, kicad_layer_name(t.layer), int(t.width)))
    return out


def format_rows(rows: List[dict], tr) -> List[str]:
    lines = []
    for r in rows:
        if r["off_layer"]:
            lines.append(tr("chk_offlayer", cls=r["cls"], layer=r["layer"], n=r["n"]))
        else:
            lines.append(tr("chk_row", cls=r["cls"], layer=r["layer"], n=r["n"], bad=r["bad"],
                            t=r["target"] / 1e6, lo=r["wmin"] / 1e6, hi=r["wmax"] / 1e6))
    return lines
