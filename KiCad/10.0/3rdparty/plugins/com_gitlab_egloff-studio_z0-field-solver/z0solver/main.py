# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Einstiegspunkt, den KiCad fuer die Plugin-Aktion startet (plugin.json).

Diese Datei muss auf jedem Interpreter parsen und starten, den ein Anwender
in KiCad eingestellt haben koennte - also keine modernen Sprachfeatures vor
der Versionspruefung und keine Plugin-Imports auf Modulebene.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from typing import Any, Optional

REQUIRED_PYTHON = (3, 9)
TITLE = "Z0 Field Solver"
PLUGIN_ID = "com.gitlab.egloff-studio.z0-field-solver"


def _say(text: str, error: bool = True) -> None:
    """Meldung an den Anwender: wx-Box wenn moeglich, sonst Konsole (stderr,
    das KiCad 10.0.1+ in den Statusmeldungen zeigt), sonst HTML im Browser."""
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    try:
        import wx
        app = wx.App(False)
        style = wx.OK | (wx.ICON_ERROR if error else wx.ICON_INFORMATION)
        wx.MessageBox(text, TITLE, style)
        app.Destroy()
        return
    except Exception:
        pass
    try:
        import webbrowser
        fd, path = tempfile.mkstemp(suffix=".html", prefix="z0solver-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("<html><head><meta charset='utf-8'></head><body><pre>"
                     + text.replace("&", "&amp;").replace("<", "&lt;")
                     + "</pre></body></html>")
        webbrowser.open("file://" + path)
    except Exception:
        pass


def _check_python() -> Optional[str]:
    if sys.version_info[:2] >= REQUIRED_PYTHON:
        return None
    found = ".".join(str(p) for p in sys.version_info[:3])
    return (
        "Das Z0-Field-Solver-Plugin braucht Python 3.9 oder neuer. KiCad startet es mit "
        f"Python {found} ({sys.executable}).\n\n"
        "In KiCad: Preferences > Plugins > Python Interpreter auf KICads eigenes Python "
        "zuruecksetzen (Detect Automatically), dann rechte Maustaste auf das Plugin unter "
        "Preferences > Plugins > Action Plugins > Recreate Plugin Environment und KiCad neu starten."
    )


def connect():
    """(kicad, board, project) vom laufenden KiCad, oder Fehlermeldung."""
    try:
        import kipy
    except ImportError:
        raise RuntimeError(
            "Die Bibliothek kicad-python fehlt in der Plugin-Umgebung. "
            "Plugin unter Preferences > Plugins > Action Plugins mit rechter Maustaste "
            "auf 'Recreate Plugin Environment' setzen und KiCad neu starten.")
    try:
        kicad = kipy.KiCad()
    except Exception as exc:
        raise RuntimeError(
            "Keine Verbindung zu KiCad. Die KiCad-API unter "
            "Preferences > Plugins ('Enable KiCad API') einschalten, "
            "dann die Aktion erneut starten. " + f"({type(exc).__name__})")
    try:
        board = kicad.get_board()
    except Exception:
        raise RuntimeError(
            "KiCad liefert kein offenes Board. Bitte das Board im PCB-Editor oeffnen "
            "und die Aktion von dort starten.")
    project = None
    try:
        project = board.get_project()
    except Exception:
        pass
    return kicad, board, project


def run() -> int:
    problem = _check_python()
    if problem:
        _say(problem)
        return 2
    try:
        kicad, board, project = connect()
    except RuntimeError as exc:
        _say(str(exc))
        return 2
    try:
        from stackup import from_kipy
        stackup = from_kipy(board.get_stackup())
    except Exception:
        _say("Das Stackup des Boards konnte nicht gelesen werden:\n\n" + traceback.format_exc())
        return 2
    if not any(l["type"] == "copper" for l in stackup["layers"]):
        _say("Im Board-Stackup ist keine Kupferlage. Bitte in Board Setup > Board Stackup "
             "den Lagenaufbau mit Dicken und eps_r pflegen.")
        return 2

    class_names = []
    if project is not None:
        try:
            class_names = [getattr(c, "name", None) or c.proto.name
                           for c in project.get_net_classes()]
        except Exception:
            class_names = []
    if not class_names:
        class_names = ["Default"]

    try:
        import wx
        from dialog import Z0Dialog
    except Exception:
        _say("wxPython ist in dieser Plugin-Umgebung nicht verfuegbar "
             "(unter Linux: Paket python3-wxgtk4.0 installieren, dann die Plugin-Umgebung "
             "neu erstellen). Ohne wx kann der Dialog nicht starten.")
        return 2

    app = wx.GetApp() or wx.App(False)
    dialog = Z0Dialog(None, stackup, class_names, kicad, board, project)
    dialog.ShowModal()
    dialog.Destroy()
    return 0


if __name__ == "__main__":
    sys.exit(run())
else:
    # KiCad startet den entrypoint als Skript; manche Versionen importieren ihn.
    run()
