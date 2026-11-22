# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Aus dem Board-Stackup (neutrales Dict, siehe stackup.py) wird je
Kupferlage ein Rechenmodell: Microstrip auf Aussenlagen, Stripline auf
Innenlagen zwischen den naechsten Bezugslagen.

Regeln (wie ueblich in der Praxis):
  - Eine Hoehe ist das Dielektrikum zwischen Leiterlage und Bezugslage;
    dazwischen liegende Signallagen-Kupfer zaehlt zur Hoehe (verharzt),
    aber nicht in das gewichtete eps_r.
  - eps_r mehrerer Prepregs wird dickengewichtet gemittelt.
  - Aussenlagen bekommen optional Loetstopplack (aus dem Stackup).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

NM_PER_MM = 1e6


@dataclass
class LayerModel:
    """Rechenmodell einer Kupferlage (alle Laengen in mm)."""
    layer: str                 # KiCad-Lagenname, z. B. "F.Cu"
    kind: str                  # "microstrip" | "stripline"
    t_mm: float
    h_mm: float
    er1: float
    h2_mm: float = 0.0
    er2: float = 0.0
    mask_t_mm: float = 0.0
    mask_er: float = 3.3
    ref_below: Optional[str] = None
    ref_above: Optional[str] = None
    problems: List[str] = field(default_factory=list)


def _weighted(er_parts):
    """Dickengewichtetes eps_r; None wenn eine Angabe fehlt."""
    num = den = 0.0
    for thickness_nm, er in er_parts:
        if thickness_nm is None or er is None:
            return None
        num += thickness_nm * er
        den += thickness_nm
    return num / den if den else None


def build_models(stackup: dict, planes: List[str]) -> Dict[str, LayerModel]:
    """Fuer jede Kupferlage das Modell ableiten.

    stackup: {"layers": [...]} top->bottom (Kupfer, Dielektrikum, Soldermask)
    planes:  Lagen-IDs der Kupferlagen, die als Bezugslage gelten.
    Rueckgabe: {Lagen-ID: LayerModel} - Lagen mit fehlenden Angaben kommen
    mit Eintraegen in .problems zurueck.
    """
    layers = stackup.get("layers", [])
    coppers = [l for l in layers if l.get("type") == "copper"]
    masks = {"top": None, "bottom": None}
    for l in layers:
        if l.get("type") == "soldermask":
            masks[l.get("side", "top")] = l

    # Index jeder Kupferlage in der Gesamtliste
    pos = {id(l): i for i, l in enumerate(layers)}

    def dielectric_between(i_a, i_b):
        """Dielektrikum zwischen zwei Listenelementen: (Hoehe mm, eps_r)."""
        height_nm = 0
        er_parts = []
        for l in layers[min(i_a, i_b) + 1: max(i_a, i_b)]:
            if l.get("type") == "dielectric":
                for ply in l.get("sublayers", []) or [l]:
                    t = ply.get("thicknessNm")
                    if t is None:
                        return None, None
                    height_nm += t
                    er_parts.append((t, ply.get("epsilonR")))
            elif l.get("type") == "copper":
                t = l.get("thicknessNm")
                if t is None:
                    return None, None
                height_nm += t  # verharztes Kupfer zaehlt zur Hoehe
        er = _weighted(er_parts)
        if er is None:
            return None, None
        return height_nm / NM_PER_MM, er

    def nearest_plane(i, direction):
        j = i + direction
        while 0 <= j < len(layers):
            l = layers[j]
            if l.get("type") == "copper":
                if l.get("id") in planes:
                    return j, l
            j += direction
        return None, None

    models: Dict[str, LayerModel] = {}
    n_cu = len(coppers)
    for idx, cu in enumerate(coppers):
        i = pos[id(cu)]
        lid = cu.get("id") or cu.get("name")
        outer = idx == 0 or idx == n_cu - 1
        problems = []
        t_nm = cu.get("thicknessNm")
        if t_nm is None:
            problems.append("Kupferdicke fehlt im Stackup")
        t_mm = (t_nm or 35000) / NM_PER_MM

        j_b, plane_b = nearest_plane(i, +1)
        j_a, plane_a = nearest_plane(i, -1)

        if outer:
            ref = plane_b if idx == 0 else plane_a
            j_ref = j_b if idx == 0 else j_a
            if ref is None:
                problems.append("keine Bezugslage gefunden")
                h_mm, er = None, None
            else:
                h_mm, er = dielectric_between(i, j_ref)
                if h_mm is None:
                    problems.append("Dielektrikum ohne Dicke/epsilon_r")
            mask = masks["top" if idx == 0 else "bottom"]
            mask_t = ((mask or {}).get("thicknessNm") or 0) / NM_PER_MM
            models[lid] = LayerModel(
                layer=lid, kind="microstrip", t_mm=t_mm,
                h_mm=h_mm or 0.15, er1=er or 4.5,
                mask_t_mm=mask_t, mask_er=(mask or {}).get("epsilonR") or 3.3,
                ref_below=(ref or {}).get("id"), problems=problems,
            )
        else:
            if plane_a is None or plane_b is None:
                problems.append("Innenlage: Bezugslage nur einseitig oder gar nicht gewaehlt")
            h1, er1 = (dielectric_between(i, j_b) if plane_b else (None, None))
            h2, er2 = (dielectric_between(i, j_a) if plane_a else (None, None))
            if h1 is None or h2 is None:
                problems.append("Dielektrikum ohne Dicke/epsilon_r")
            models[lid] = LayerModel(
                layer=lid, kind="stripline", t_mm=t_mm,
                h_mm=h1 or 0.15, er1=er1 or 4.5,
                h2_mm=h2 or 0.15, er2=er2 or 4.5,
                ref_below=(plane_b or {}).get("id"),
                ref_above=(plane_a or {}).get("id"),
                problems=problems,
            )
    return models
