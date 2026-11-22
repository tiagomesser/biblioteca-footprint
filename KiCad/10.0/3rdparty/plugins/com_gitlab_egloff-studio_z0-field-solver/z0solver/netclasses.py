# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Netzklassen lesen, Aenderungen vorbereiten, schreiben, verifizieren.

Schreiben ueber die IPC API (kicad-python):
  - KiCad <= 10.0.6 hat einen Fehler, der beim Setzen von Netzklassen
    still eine Klasse fallen laesst (Fix d622c37a, danach im 10.0-Zweig).
    Deshalb: Schreiben nur bei KiCad > 10.0.6, sonst Werte zur manuellen
    Eingabe anzeigen.
  - Weg 1: Project.set_net_classes (kicad-python >= 0.9)
  - Weg 2: rohes SetNetClasses-Kommando (Fallback)
  - Danach wird zurueckgelesen und verglichen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Felder, die das Plugin schreibt (Board-Teil der Netzklasse).
# Interne Feldnamen -> Attributnamen im echten KiCad-Protokoll (live gegen KiCad 10.0.6 geprueft:
# das Feld heisst dort diff_pair_track_width, nicht diff_pair_width).
PROTO_ATTR = {"diff_pair_width": "diff_pair_track_width"}
FIELDS = ("track_width", "diff_pair_width", "diff_pair_gap", "diff_pair_via_gap")

FIELD_LABELS = {
    "track_width": "Bahnbreite",
    "diff_pair_width": "Diff-Paar-Breite",
    "diff_pair_gap": "Diff-Paar-Gap",
    "diff_pair_via_gap": "Diff-Paar-Via-Gap",
}

#: KiCad aelter oder gleich dieser Version darf nicht beschrieben werden.
FIRST_SAFE_WRITE_AFTER = (10, 0, 6)

NM_PER_MM = 1e6


class NetClassWriteError(Exception):
    pass


def mm_to_nm(mm: float) -> int:
    return int(round(mm * NM_PER_MM))


def fmt_nm(nm: Optional[int]) -> str:
    if nm is None:
        return "-"
    s = f"{nm / NM_PER_MM:.4f}".rstrip("0").rstrip(".")
    return f"{s} mm"


def _proto(netclass: Any) -> Any:
    return getattr(netclass, "proto", netclass)


def field_values(netclass: Any) -> Dict[str, Optional[int]]:
    """Aktuelle Werte (nm) der schreibbaren Felder; None = nicht gesetzt."""
    proto = _proto(netclass)
    board = getattr(proto, "board", proto)
    out: Dict[str, Optional[int]] = {}
    for name in FIELDS:
        holder = getattr(board, PROTO_ATTR.get(name, name), None)
        value = getattr(holder, "value_nm", None)
        out[name] = int(value) if value else None
    return out


def read_classes(project: Any) -> List[Any]:
    """Netzklassen als eigenstaendige Kopien (proto)."""
    from kipy.proto.common.types import project_settings_pb2

    classes = []
    for netclass in project.get_net_classes():
        message = project_settings_pb2.NetClass()
        message.CopyFrom(_proto(netclass))
        classes.append(message)
    return classes


@dataclass
class FieldChange:
    netclass: str
    field: str
    current_nm: Optional[int]
    proposed_nm: int

    def text(self) -> str:
        label = FIELD_LABELS.get(self.field, self.field)
        return f"{self.netclass}: {label} {fmt_nm(self.current_nm)} -> {fmt_nm(self.proposed_nm)}"


@dataclass
class Preview:
    changes: List[FieldChange] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes

    def text(self) -> str:
        lines = [c.text() for c in self.changes]
        lines += self.notes
        return "\n".join(lines) if lines else "Keine Aenderungen noetig."

    def manual_text(self) -> str:
        lines = ["Werte zur manuellen Eingabe in Board Setup > Netzklassen:", ""]
        for c in self.changes:
            label = FIELD_LABELS.get(c.field, c.field)
            lines.append(f"{c.netclass}\t{label}\t{fmt_nm(c.proposed_nm)}")
        return "\n".join(lines)


def make_preview(current: List[Any], proposals: Dict[str, Dict[str, int]]) -> Preview:
    """proposals: {Netzklasse: {Feld: nm}} -> Preview der echten Aenderungen."""
    by_name = {c.name: c for c in current}
    preview = Preview()
    for name, fields in proposals.items():
        if name not in by_name:
            preview.notes.append(f"Netzklasse {name} gibt es im Projekt nicht mehr - ausgelassen.")
            continue
        existing = field_values(by_name[name])
        for fname, nm in fields.items():
            if fname not in FIELDS:
                continue
            if existing.get(fname) != nm:
                preview.changes.append(FieldChange(name, fname, existing.get(fname), nm))
    return preview


# ── Versions-Gate ────────────────────────────────────────────────────────────


def kicad_version(kicad: Any) -> Optional[Tuple[int, int, int]]:
    getter = getattr(kicad, "get_version", None)
    if not callable(getter):
        return None
    try:
        reply = getter()
        version = (int(reply.major), int(reply.minor), int(reply.patch))
    except Exception:
        return None
    return None if version == (0, 0, 0) else version


def write_blocked_reason(version: Optional[Tuple[int, int, int]]) -> Optional[str]:
    if version is None:
        return ("KiCads Version ist nicht lesbar - zur Sicherheit wird nicht geschrieben. "
                "Die Werte stehen zur manuellen Eingabe bereit.")
    if tuple(version[:3]) <= FIRST_SAFE_WRITE_AFTER:
        v = ".".join(str(p) for p in version[:3])
        return (f"KiCad {v} aelter/gleich 10.0.6: diese Version verliert beim Schreiben "
                f"von Netzklassen ueber die API still eine Klasse. Bitte auf eine neuere "
                f"10.0.x aktualisieren - die Werte stehen zur manuellen Eingabe bereit.")
    return None


# ── Schreiben ────────────────────────────────────────────────────────────────


def _explicit_copy(netclass_proto: Any, values: Dict[str, Optional[int]]) -> Any:
    """Klasse als Ganzes mit neuen Werten, NCT_EXPLICIT, ohne constituents."""
    from kipy.proto.common.types import project_settings_pb2

    message = project_settings_pb2.NetClass()
    message.CopyFrom(netclass_proto)
    for name, nm in values.items():
        if name not in FIELDS:
            raise ValueError(f"{name} ist kein schreibbares Feld")
        if nm is None:
            message.board.ClearField(PROTO_ATTR.get(name, name))
        else:
            getattr(message.board, PROTO_ATTR.get(name, name)).value_nm = int(nm)
    message.type = project_settings_pb2.NetClassType.NCT_EXPLICIT
    message.ClearField("constituents")
    return message


def _send(project: Any, classes: List[Any]) -> str:
    """Ganze Klassen mit MMM_MERGE senden; gibt den benutzten Weg zurueck."""
    from kipy.proto.common.types import MapMergeMode

    if callable(getattr(project, "set_net_classes", None)):
        from kipy.project_types import NetClass

        project.set_net_classes([NetClass(c) for c in classes],
                                merge_mode=MapMergeMode.MMM_MERGE)
        return "set_net_classes"
    from google.protobuf.empty_pb2 import Empty
    from kipy.proto.common.commands import project_commands_pb2

    if not hasattr(project_commands_pb2, "SetNetClasses"):
        raise NetClassWriteError(
            "Die installierte kicad-python-Bibliothek kann keine Netzklassen schreiben. "
            "Bitte kicad-python aktualisieren.")
    command = project_commands_pb2.SetNetClasses()
    command.merge_mode = MapMergeMode.MMM_MERGE
    command.net_classes.extend(classes)
    project._kicad.send(command, Empty)
    return "SetNetClasses-Kommando"


def apply(project: Any, preview: Preview, current: List[Any]) -> Tuple[bool, str]:
    """Preview schreiben und durch Ruecklesen verifizieren."""
    if preview.empty:
        return True, "Keine Aenderungen noetig."
    by_name = {c.name: c for c in current}
    grouped: Dict[str, Dict[str, int]] = {}
    for change in preview.changes:
        grouped.setdefault(change.netclass, {})[change.field] = change.proposed_nm
    classes = []
    for name, values in grouped.items():
        if name not in by_name:
            return False, f"Netzklasse {name} ist nicht mehr im Projekt."
        classes.append(_explicit_copy(by_name[name], values))
    try:
        method = _send(project, classes)
    except NetClassWriteError as exc:
        return False, str(exc)
    except Exception as exc:  # API-Fehler sauber melden
        return False, f"KiCad hat das Schreiben abgelehnt: {type(exc).__name__}: {exc}"

    time.sleep(0.3)
    try:
        after = {c.name: field_values(c) for c in read_classes(project)}
    except Exception as exc:
        return False, (f"Geschrieben ueber {method}, aber das Zuruecklesen scheiterte: "
                       f"{type(exc).__name__}. Bitte in Board Setup pruefen.")
    problems = []
    for change in preview.changes:
        got = after.get(change.netclass, {}).get(change.field)
        if got != change.proposed_nm:
            problems.append(
                f"{change.netclass}: {FIELD_LABELS.get(change.field)} ist "
                f"{fmt_nm(got)} statt {fmt_nm(change.proposed_nm)}")
    if problems:
        return False, ("KiCad hat geantwortet, aber die Werte stehen nicht wie gesendet: "
                       + "; ".join(problems)
                       + ". Bitte in Board Setup pruefen.")
    return True, f"{len(preview.changes)} Werte in {len(grouped)} Netzklasse(n) geschrieben ({method}) und verifiziert."
