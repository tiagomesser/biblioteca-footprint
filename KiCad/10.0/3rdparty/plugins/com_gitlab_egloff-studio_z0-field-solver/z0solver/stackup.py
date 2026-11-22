# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Board-Stackup aus KiCad lesen (kicad-python / IPC API) und in ein
neutrales Dict verwandeln, damit alles danach ohne KiCad testbar ist.

Form::
    {"boardThicknessNm": 1600000 | None,
     "layers": [                        # von oben nach unten
       {"name": "F.Mask", "type": "soldermask", "side": "top",
        "thicknessNm": 10000, "epsilonR": 3.3},
       {"name": "F.Cu", "id": "F.Cu", "type": "copper", "thicknessNm": 35000},
       {"name": "dielectric 1", "type": "dielectric",
        "sublayers": [{"material": "FR4", "thicknessNm": 1510000,
                       "epsilonR": 4.5, "lossTangent": 0.02}]},
       ...]}

Dicke 0 / fehlende Angaben werden zu None, damit das Modell sie als
Fehler benennen kann statt mit 0 zu rechnen.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _nm_or_none(value) -> Optional[int]:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def kicad_layer_name(board_layer: int) -> str:
    """BL_F_Cu -> F.Cu, BL_In1_Cu -> In1.Cu, BL_F_Mask -> F.Mask."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    name = BoardLayer.Name(board_layer)
    if name.startswith("BL_"):
        name = name[3:]
    return name.replace("_", ".", 1)


def from_kipy(board_stackup: Any) -> Dict[str, Any]:
    """kipy ``Board.get_stackup()`` -> neutrales Stackup-Dict."""
    from kipy.board import BoardStackup
    from kipy.proto.board import board_pb2

    if isinstance(board_stackup, board_pb2.BoardStackup):
        board_stackup = BoardStackup(board_stackup)

    layers: List[Dict[str, Any]] = []
    for layer in board_stackup.layers:
        kind = layer.type
        if kind == board_pb2.BSLT_COPPER:
            kicad_id = kicad_layer_name(layer.layer)
            layers.append({
                "name": layer.user_name or kicad_id,
                "id": kicad_id,
                "type": "copper",
                "thicknessNm": _nm_or_none(layer.thickness),
            })
        elif kind == board_pb2.BSLT_DIELECTRIC:
            sublayers = []
            details = layer.dielectric
            if details is not None and getattr(details, "layers", None):
                for ply in details.layers:
                    sublayers.append({
                        "material": ply.material_name,
                        "thicknessNm": _nm_or_none(ply.thickness),
                        "epsilonR": float(ply.epsilon_r) if ply.epsilon_r else None,
                        "lossTangent": float(ply.loss_tangent),
                    })
            if not sublayers:
                sublayers.append({
                    "material": "",
                    "thicknessNm": _nm_or_none(layer.thickness),
                    "epsilonR": None,
                    "lossTangent": 0.0,
                })
            layers.append({
                "name": layer.user_name or f"dielectric {len([l for l in layers if l['type']=='dielectric']) + 1}",
                "type": "dielectric",
                "sublayers": sublayers,
            })
        elif kind == board_pb2.BSLT_SOLDERMASK:
            side = "top" if "F" in kicad_layer_name(layer.layer).split(".")[0] else "bottom"
            er = None
            details = layer.dielectric
            if details is not None and getattr(details, "layers", None):
                ers = [p.epsilon_r for p in details.layers if p.epsilon_r]
                er = float(ers[0]) if ers else None
            layers.append({
                "name": kicad_layer_name(layer.layer),
                "type": "soldermask",
                "side": side,
                "thicknessNm": _nm_or_none(layer.thickness),
                "epsilonR": er or 3.3,
            })
        # Silkscreen / Solderpaste: elektrisch irrelevant

    total: Optional[int] = 0
    for entry in layers:
        parts = entry["sublayers"] if entry["type"] == "dielectric" else [entry]
        for part in parts:
            if part.get("thicknessNm") is None:
                total = None
                break
            total += part["thicknessNm"]
        if total is None:
            break
    return {"boardThicknessNm": total, "layers": layers}


def describe(stackup: Dict[str, Any]) -> List[str]:
    """Lesbare Zeilen fuer UI/Report."""
    lines = []
    for l in stackup.get("layers", []):
        if l["type"] == "copper":
            t = l.get("thicknessNm")
            lines.append(f"{l['name']}: Kupfer {t/1000:.0f} um" if t else f"{l['name']}: Kupfer (Dicke?)")
        elif l["type"] == "dielectric":
            for ply in l.get("sublayers", []):
                t = ply.get("thicknessNm")
                er = ply.get("epsilonR")
                mat = ply.get("material") or l["name"]
                t_txt = f"{t/1e6:.4f} mm" if t else "? mm"
                er_txt = f"{er:g}" if er else "?"
                lines.append(f"{l['name']}: {mat} {t_txt}, eps_r {er_txt}")
        elif l["type"] == "soldermask":
            t = l.get("thicknessNm")
            lines.append(f"{l['name']}: Loetstopplack {t/1000:.0f} um" if t else f"{l['name']}: Loetstopplack")
    return lines
