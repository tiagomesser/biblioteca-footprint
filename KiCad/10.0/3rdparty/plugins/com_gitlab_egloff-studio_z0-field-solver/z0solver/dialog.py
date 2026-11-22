# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Der Dialog des Plugins (wxPython, deutsch), Version 2.

Workflow in drei Schritten:
  1. Lagenaufbau: Stackup aus dem Board, Dielektrika editierbar, Presets,
     Bezugslagen waehlbar, Querschnitt als Zeichnung.
  2. Impedanz-Profile: benannte Ziele (z. B. "SE50", "USB 90 Ohm diff"),
     optional einer Netzklasse zugeordnet.
  3. Ergebnisse: fuer jedes Profil werden Breite/Gap fuer ALLE Signallagen
     auf einmal gerechnet (kein stiller Lagenwechsel wie in Altium),
     Querschnitt + Z(w)-Kurve, dann: auf Netzklassen anwenden,
     .kicad_dru schreiben, HTML-Bericht.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import wx
import wx.grid

from fieldsolver import Structure
from geometry import solve_width, solve_gap, solve_width_for_zdiff, SolveError
from layermodel import build_models
from netclasses import (make_preview, apply, read_classes, kicad_version,
                        write_blocked_reason, mm_to_nm, fmt_nm)
from rules import build_dru
from check import check_tracks, collect_tracks, format_rows
from stackup import describe as describe_stackup
from report import build_html, result_lines
from i18n import tr, set_lang, get_lang, LANGS, LANG_NAMES

MM = 1e-3
TITLE = "Z0 Field Solver"

#: Stackup-Presets: Name -> Liste (Dicke mm, eps_r) je Dielektrikum von oben
PRESETS = {
    # (Dicke mm, eps_r) je Dielektrikum von oben; Quellen: docs/presets-quellen.md
    "JLCPCB 4-layer JLC04161H-7628 (1.6 mm)": [(0.2104, 4.4), (1.065, 4.6), (0.2104, 4.4)],
    "JLCPCB 4-layer JLC04161H-3313 (1.6 mm)": [(0.0994, 4.1), (1.265, 4.6), (0.0994, 4.1)],
    "PCBWay 4-layer 1.6 mm": [(0.11, 4.29), (1.13, 3.96), (0.11, 4.29)],
    "PCBWay 4-layer 1.2 mm": [(0.11, 4.29), (0.73, 3.96), (0.11, 4.29)],
    "PCBWay 4-layer 1.0 mm": [(0.175, 4.29), (0.53, 3.96), (0.175, 4.29)],
    "Multi-CB 4-layer 1.6 mm (4L-01)": [(0.140, 4.00), (1.2, 4.60), (0.140, 4.00)],
    "Multi-CB 4-layer 1.0 mm (4L-02)": [(0.230, 4.25), (0.4, 4.50), (0.230, 4.25)],
    "OSH Park 4-layer (FR408-HR)": [(0.1999, 3.61), (0.9906, 3.61), (0.1999, 3.61)],
}

COL_NAVY = wx.Colour(28, 52, 92)
COL_BG = wx.Colour(246, 247, 249)
COL_ROW_ALT = wx.Colour(243, 246, 251)
COL_OK = wx.Colour(226, 244, 230)
COL_ERR = wx.Colour(253, 228, 228)
COL_STALE = wx.Colour(255, 244, 214)
COL_CU = wx.Colour(184, 115, 51)
COL_PRE = wx.Colour(238, 224, 177)
COL_CORE = wx.Colour(224, 200, 140)
COL_MASK = wx.Colour(46, 125, 50)
COL_SEL = wx.Colour(30, 100, 200)


def structure_from_model(model, w_mm, gap_mm, use_mask, quality):
    return Structure(
        kind=model.kind,
        w=w_mm * MM,
        t=model.t_mm * MM,
        h=model.h_mm * MM,
        er1=model.er1,
        h2=model.h2_mm * MM,
        er2=model.er2,
        gap=(gap_mm * MM if gap_mm else None),
        mask_t=(model.mask_t_mm * MM if use_mask else 0.0),
        mask_er=model.mask_er,
        quality=quality,
    )



def _font(base, size=None, bold=False):
    f = wx.Font(base)
    if size:
        f.SetPointSize(size)
    if bold:
        f.SetWeight(wx.FONTWEIGHT_BOLD)
    return f


def _style_grid(g, editable_cols=()):
    g.SetDefaultRowSize(26, True)
    g.SetColLabelSize(28)
    g.SetRowLabelSize(0)
    g.EnableDragGridSize(False)
    g.EnableDragRowSize(False)
    g.SetLabelBackgroundColour(wx.Colour(226, 232, 242))
    g.SetLabelFont(_font(g.GetLabelFont(), bold=True))
    g.SetGridLineColour(wx.Colour(215, 220, 228))
    g.SetDefaultCellOverflow(False)
    g.SetMargins(0, 0)


def _zebra(g):
    for r in range(g.GetNumberRows()):
        g.SetRowAttr(r, None) if False else None
        for c in range(g.GetNumberCols()):
            g.SetCellBackgroundColour(r, c, COL_ROW_ALT if r % 2 else wx.WHITE)


def _fit_cols(g, widths, stretch_col=None):
    """Feste Mindestbreiten; die Stretch-Spalte bekommt den Rest."""
    for c, w in enumerate(widths):
        g.SetColSize(c, w)
    if stretch_col is not None:
        total = g.GetClientSize().width
        used = sum(widths) - widths[stretch_col]
        if total > used + widths[stretch_col]:
            g.SetColSize(stretch_col, total - used)


class StackupPanel(wx.Panel):
    """Massstabsgetreuer Lagenquerschnitt mit Beschriftung und Legende."""

    def __init__(self, parent, dialog):
        super().__init__(parent)
        self.dlg = dialog
        self.SetMinSize((260, 300))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.WHITE_BRUSH)
        dc.Clear()
        layers = self.dlg.stackup.get("layers", [])
        base = self.GetFont()
        small = _font(base, max(7, base.GetPointSize() - 1))
        dc.SetFont(small)
        if not layers:
            return
        legend_h = 40
        x0, x1 = 12, w - 120
        top, bot = 8, h - legend_h - 8
        # Darstellungsdicke: Wurzel-gestaucht, damit dünnes Kupfer sichtbar bleibt
        def disp(l):
            t = (l.get("thicknessNm") or 0) / 1e6
            return max(0.03, t) ** 0.5
        total = sum(disp(l) for l in layers) or 1
        gap = 2
        avail = (bot - top) - gap * len(layers)
        y = top
        for l in layers:
            lh = max(8, int(avail * disp(l) / total))
            typ = l.get("type")
            mat = (l.get("material") or "").lower()
            col = {"copper": COL_CU, "soldermask": COL_MASK}.get(
                typ, COL_CORE if "core" in mat else COL_PRE)
            dc.SetBrush(wx.Brush(col))
            dc.SetPen(wx.Pen(wx.Colour(120, 120, 120), 1))
            dc.DrawRectangle(x0, y, x1 - x0, lh)
            t = (l.get("thicknessNm") or 0) / 1e6
            if typ == "copper":
                label = f"{l.get('id')}   {t*1000:.0f} µm"
            elif typ == "soldermask":
                label = f"{tr('lg_mask')}   {t*1000:.0f} µm"
            else:
                er = l.get("epsilonR")
                label = f"{l.get('material') or ''}  {t:.3f} mm" + (f"  εr {er:.2f}" if er else "")
            dc.SetTextForeground(wx.WHITE if typ in ("copper", "soldermask") else wx.BLACK)
            tw, th = dc.GetTextExtent(label)
            if th <= lh or typ == "dielectric":
                dc.DrawText(label, x0 + 6, y + max(0, (lh - th) // 2))
            else:
                dc.SetTextForeground(wx.BLACK)
                dc.DrawText(label, x1 + 6, y + max(0, (lh - th) // 2))
            if self.dlg.sel_layer and l.get("id") == self.dlg.sel_layer:
                dc.SetPen(wx.Pen(COL_SEL, 3))
                dc.SetBrush(wx.TRANSPARENT_BRUSH)
                dc.DrawRectangle(x0 - 2, y - 1, x1 - x0 + 4, lh + 2)
            y += lh + gap
        # Legende
        items = [(COL_CU, tr("lg_copper")), (COL_PRE, tr("lg_prepreg")),
                 (COL_CORE, tr("lg_core")), (COL_MASK, tr("lg_mask"))]
        lx, ly = x0, h - legend_h + 4
        dc.SetTextForeground(wx.Colour(70, 70, 70))
        for col, name in items:
            dc.SetBrush(wx.Brush(col))
            dc.SetPen(wx.Pen(wx.Colour(120, 120, 120), 1))
            dc.DrawRectangle(lx, ly, 12, 12)
            dc.DrawText(name, lx + 16, ly - 1)
            lx += 20 + dc.GetTextExtent(name).width + 8
            if lx > w - 60:
                lx, ly = x0, ly + 16
        dc.DrawText(tr("top"), w - 40, 2)
        dc.DrawText(tr("bottom"), w - 52, bot - 12)


class ResultPanel(wx.Panel):
    """Querschnitt (mit Massen) und Z(w)-Kurve des gewählten Ergebnisses."""

    def __init__(self, parent, dialog):
        super().__init__(parent)
        self.dlg = dialog
        self.curve: List = []
        self.curve_target: Optional[float] = None
        self.SetMinSize((300, 230))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def _dim(self, dc, x1, x2, y, text):
        dc.SetPen(wx.Pen(wx.Colour(60, 60, 60), 1))
        dc.DrawLine(x1, y, x2, y)
        dc.DrawLine(x1, y - 3, x1, y + 3)
        dc.DrawLine(x2, y - 3, x2, y + 3)
        tw = dc.GetTextExtent(text).width
        dc.DrawText(text, (x1 + x2) // 2 - tw // 2, y - 15)

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.WHITE_BRUSH)
        dc.Clear()
        dc.SetFont(_font(self.GetFont(), max(7, self.GetFont().GetPointSize() - 1)))
        dc.SetTextForeground(wx.BLACK)
        xo = int(w * 0.52)          # rechte Haelfte: Kurve
        w = xo                      # linke Haelfte: Querschnitt
        r = self.dlg.sel_result
        m = self.dlg.models.get(r["layer"]) if r else None
        if not r or not m:
            dc.SetTextForeground(wx.Colour(120, 120, 120))
            dc.DrawText(tr("no_result"), 12, 12)
        else:
            wmm = r.get("width_mm") or 0.2
            gmm = r.get("gap_mm") or 0.0
            span_mm = (2 * wmm + gmm) if gmm else wmm
            wide = span_mm + 2 * max(m.h_mm, 0.15) * 2
            scale = (w - 60) / wide
            hs = max(8.0, m.h_mm * scale)
            ts = max(4, int(m.t_mm * scale))
            cx = w // 2
            gy = 56
            ref_y = gy + ts + int(hs)
            dc.SetPen(wx.Pen(wx.Colour(100, 100, 100), 1))
            # Dielektrikum
            dc.SetBrush(wx.Brush(COL_PRE))
            dc.DrawRectangle(20, gy + ts, w - 40, int(hs))
            dc.SetBrush(wx.Brush(COL_CU))
            dc.DrawRectangle(20, ref_y, w - 40, max(4, ts))      # Bezug unten
            if m.kind == "stripline":
                h2 = max(8.0, m.h2_mm * scale)
                dc.SetBrush(wx.Brush(COL_PRE))
                dc.DrawRectangle(20, gy - int(h2), w - 40, int(h2))
                dc.SetBrush(wx.Brush(COL_CU))
                dc.DrawRectangle(20, gy - int(h2) - max(4, ts), w - 40, max(4, ts))
            ww = max(5, int(wmm * scale))
            gp = int(gmm * scale)
            left = cx - (ww + gp + ww) // 2 if gmm else cx - ww // 2
            dc.SetBrush(wx.Brush(COL_SEL))
            dc.DrawRectangle(left, gy, ww, ts)
            if gmm:
                dc.DrawRectangle(left + ww + gp, gy, ww, ts)
            # Masse
            dc.SetTextForeground(wx.Colour(40, 40, 40))
            self._dim(dc, left, left + ww, gy - 8, f"w {wmm:.3f}")
            if gmm:
                self._dim(dc, left + ww, left + ww + gp, gy + ts + 14, f"s {gmm:.3f}")
            dc.DrawText(f"h {m.h_mm:.3f}", w - 20 - dc.GetTextExtent(f"h {m.h_mm:.3f}").width,
                        gy + ts + int(hs) // 2 - 6)
            dc.SetTextForeground(wx.BLACK)
            dc.SetFont(_font(self.GetFont(), bold=True))
            dc.DrawText(f"{r['profile']}  {tr('zoom_on')}  {r['layer']}", 12, 6)
        # Kurve
        dc.SetFont(_font(self.GetFont(), max(7, self.GetFont().GetPointSize() - 1)))
        W = self.GetClientSize().width
        y0, yb = 26, h - 24
        x0c = xo + 40
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
        dc.DrawLine(xo, 6, xo, h - 6)
        dc.SetPen(wx.Pen(wx.Colour(90, 90, 90), 1))
        dc.DrawLine(x0c, y0, x0c, yb)
        dc.DrawLine(x0c, yb, W - 12, yb)
        dc.SetFont(_font(self.GetFont(), bold=True))
        dc.SetTextForeground(wx.BLACK)
        dc.DrawText("Z(w)", xo + 12, 6)
        dc.SetFont(_font(self.GetFont(), max(7, self.GetFont().GetPointSize() - 1)))
        if self.curve:
            ws = [p[0] for p in self.curve]
            zs = [p[1] for p in self.curve]
            w0, w1 = min(ws), max(ws)
            zlo = min(zs + [self.curve_target or min(zs)])
            zhi = max(zs + [self.curve_target or max(zs)])
            span = (zhi - zlo) or 1
            px = lambda v: x0c + int((v - w0) / ((w1 - w0) or 1) * (W - 12 - x0c - 20))
            py = lambda v: yb - int((v - zlo) / span * (yb - y0 - 6))
            dc.SetPen(wx.Pen(COL_SEL, 2))
            for a, b in zip(self.curve, self.curve[1:]):
                dc.DrawLine(px(a[0]), py(a[1]), px(b[0]), py(b[1]))
            if self.curve_target:
                dc.SetPen(wx.Pen(wx.Colour(200, 40, 40), 1, wx.PENSTYLE_SHORT_DASH))
                dc.DrawLine(x0c, py(self.curve_target), W - 12, py(self.curve_target))
            dc.SetTextForeground(wx.BLACK)
            dc.DrawText(f"{w0:.2f}", px(w0) - 8, yb + 4)
            dc.DrawText(f"{w1:.2f} mm", px(w1) - 36, yb + 4)
            dc.DrawText(f"{zhi:.0f}", xo + 4, y0)
            dc.DrawText(f"{zlo:.0f} Ω", xo + 4, yb - 12)
        else:
            dc.SetTextForeground(wx.Colour(120, 120, 120))
            dc.DrawText(tr("curve_hint_short"), x0c + 8, y0 + 14)


class Z0Dialog(wx.Dialog):
    def __init__(self, parent, stackup, class_names, kicad, board, project):
        super().__init__(parent, title=TITLE, size=(1040, 800),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize((820, 600))
        for l in stackup["layers"]:
            if l.get("type") == "dielectric":
                self._normalize_diel(l)
        self.stackup_orig = {"layers": [dict(l) for l in stackup["layers"]]}
        self.stackup = stackup
        self.kicad = kicad
        self.board = board
        self.project = project
        self.class_names = list(class_names)
        self.results: List[dict] = []
        self.proposals: Dict[str, Dict[str, int]] = {}
        self.sel_layer: Optional[str] = None
        self.sel_result: Optional[dict] = None
        self.stale = False
        self.copper_ids = [l["id"] for l in stackup["layers"] if l["type"] == "copper"]
        self.planes = list(self.copper_ids)
        self.quality_idx = 1
        self.use_mask = True
        self.models = build_models(stackup, list(self.copper_ids))
        first = self.copper_ids[0]
        # Typischer Start: 50 Ohm Single-Ended, USB 90, DDR/LVDS 100
        self.profiles: List[Dict[str, Any]] = [
            {"on": True, "name": "SE50", "z0": "50", "zdiff": "", "given": "0.25",
             "mode": "gap", "layer": first, "cls": "-"},
            {"on": True, "name": "USB90", "z0": "", "zdiff": "90", "given": "0.15",
             "mode": "gap", "layer": first, "cls": "-"},
            {"on": False, "name": "DIFF100", "z0": "", "zdiff": "100", "given": "0.15",
             "mode": "gap", "layer": first, "cls": "-"},
        ]
        self._status_text = tr("st_ready")
        self._build_ui()

    # ── Aufbau (auch fuer den Sprachwechsel) ───────────────────────────────

    def _build_ui(self, keep_page=0):
        self.DestroyChildren()
        self.SetBackgroundColour(COL_BG)
        root = wx.BoxSizer(wx.VERTICAL)

        # Kopfleiste
        head = wx.Panel(self)
        head.SetBackgroundColour(COL_NAVY)
        hs = wx.BoxSizer(wx.HORIZONTAL)
        tbox = wx.BoxSizer(wx.VERTICAL)
        t1 = wx.StaticText(head, label=tr("title"))
        t1.SetFont(_font(self.GetFont(), self.GetFont().GetPointSize() + 5, True))
        t1.SetForegroundColour(wx.WHITE)
        t2 = wx.StaticText(head, label=tr("subtitle"))
        t2.SetForegroundColour(wx.Colour(190, 205, 230))
        tbox.Add(t1, 0, wx.TOP | wx.LEFT, 10)
        tbox.Add(t2, 0, wx.BOTTOM | wx.LEFT, 10)
        hs.Add(tbox, 1, wx.EXPAND)
        lang_lbl = wx.StaticText(head, label=tr("language"))
        lang_lbl.SetForegroundColour(wx.Colour(190, 205, 230))
        self.choice_lang = wx.Choice(head, choices=[LANG_NAMES[c] for c in LANGS])
        self.choice_lang.SetSelection(LANGS.index(get_lang()))
        self.choice_lang.Bind(wx.EVT_CHOICE, self._on_lang)
        hs.Add(lang_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        hs.Add(self.choice_lang, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)
        head.SetSizer(hs)
        root.Add(head, 0, wx.EXPAND)

        # Banner bei veralteten Ergebnissen
        self.banner = wx.StaticText(self, label="")
        self.banner.SetBackgroundColour(COL_STALE)
        self.banner.Hide()
        root.Add(self.banner, 0, wx.EXPAND | wx.ALL, 0)

        self.nb = wx.Notebook(self)
        self.page_stackup = self._build_stackup_page(self.nb)
        self.page_profiles = self._build_profiles_page(self.nb)
        self.page_results = self._build_results_page(self.nb)
        self.nb.AddPage(self.page_stackup, tr("tab1"))
        self.nb.AddPage(self.page_profiles, tr("tab2"))
        self.nb.AddPage(self.page_results, tr("tab3"))
        self.nb.SetSelection(keep_page)
        root.Add(self.nb, 1, wx.EXPAND | wx.ALL, 8)

        # Fusszeile: Status + Fortschritt + Aktionen
        self.gauge = wx.Gauge(self, range=100, size=(140, 14))
        self.gauge.Hide()
        self.status = wx.StaticText(self, label=self._status_text)
        srow = wx.BoxSizer(wx.HORIZONTAL)
        srow.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        srow.Add(self.gauge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        root.Add(srow, 0, wx.EXPAND | wx.BOTTOM, 4)

        art = wx.ArtProvider.GetBitmap
        def mk(label, art_id=None, primary=False):
            b = wx.Button(self, label=label)
            if art_id:
                bmp = art(art_id, wx.ART_BUTTON, (16, 16))
                if bmp.IsOk():
                    b.SetBitmap(bmp)
                    b.SetBitmapMargins((6, 0))
            if primary:
                b.SetFont(_font(b.GetFont(), bold=True))
                b.SetBackgroundColour(wx.Colour(34, 110, 200))
                b.SetForegroundColour(wx.WHITE)
                b.SetMinSize((150, 36))
            else:
                b.SetMinSize((-1, 32))
            return b
        self.btn_run = mk("&" + tr("btn_calc"), wx.ART_EXECUTABLE_FILE, True)
        self.btn_apply = mk(tr("btn_apply"), wx.ART_TICK_MARK)
        self.btn_dru = mk(tr("btn_dru"), wx.ART_FILE_SAVE)
        self.btn_report = mk(tr("btn_report"), wx.ART_NORMAL_FILE)
        self.btn_tune = mk(tr("btn_tune"), wx.ART_COPY)
        self.btn_check = mk(tr("btn_check"), wx.ART_FIND)
        self.btn_close = mk(tr("btn_close"), wx.ART_CROSS_MARK)
        have = bool(self.results)
        for b in (self.btn_apply, self.btn_dru, self.btn_report, self.btn_tune, self.btn_check):
            b.Enable(have)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.btn_run, 0, wx.ALL, 6)
        buttons.AddSpacer(14)
        for b in (self.btn_apply, self.btn_dru, self.btn_report, self.btn_tune, self.btn_check):
            buttons.Add(b, 0, wx.TOP | wx.BOTTOM | wx.RIGHT, 6)
        buttons.AddStretchSpacer()
        buttons.Add(self.btn_close, 0, wx.ALL, 6)
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        self.SetSizer(root)

        self.btn_run.Bind(wx.EVT_BUTTON, self.on_run)
        self.btn_apply.Bind(wx.EVT_BUTTON, self.on_apply)
        self.btn_dru.Bind(wx.EVT_BUTTON, self.on_dru)
        self.btn_report.Bind(wx.EVT_BUTTON, self.on_report)
        self.btn_tune.Bind(wx.EVT_BUTTON, self.on_tune)
        self.btn_check.Bind(wx.EVT_BUTTON, self.on_check)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.btn_run.SetDefault()
        self._refresh_models()
        if self.results:
            self._show_results()
        self._update_banner()
        self.Layout()

    def _on_lang(self, event):
        set_lang(LANGS[self.choice_lang.GetSelection()])
        self._read_profiles()
        self._status_text = tr("st_ready")
        page = self.nb.GetSelection()
        self._build_ui(page)

    def _set_status(self, text):
        self._status_text = text
        self.status.SetLabel(text)
        self.status.Refresh()

    def _update_banner(self):
        if self.stale and self.results:
            self.banner.SetLabel("  " + tr("stale"))
            self.banner.Show()
        else:
            self.banner.Hide()
        self.Layout()

    def _mark_stale(self):
        if self.results:
            self.stale = True
            self._update_banner()

    # ── Seite 1: Lagenaufbau ────────────────────────────────────────────────

    @staticmethod
    def _normalize_diel(l):
        subs = l.get("sublayers") or []
        ts = [p.get("thicknessNm") for p in subs]
        if subs and all(t is not None for t in ts) and sum(ts) > 0:
            tot = sum(ts)
            l["thicknessNm"] = tot
            if all(p.get("epsilonR") for p in subs):
                l["epsilonR"] = sum(p["thicknessNm"] * p["epsilonR"] for p in subs) / tot
            mats = [p.get("material") for p in subs if p.get("material")]
            if mats:
                l["material"] = mats[0]

    @staticmethod
    def _collapse(l):
        l["sublayers"] = [{"material": l.get("material", ""),
                           "thicknessNm": l["thicknessNm"],
                           "epsilonR": l["epsilonR"],
                           "lossTangent": l.get("lossTangent", 0.0)}]

    def _dielectrics(self):
        return [l for l in self.stackup["layers"] if l["type"] == "dielectric"]

    def _box(self, parent, key):
        return wx.StaticBoxSizer(wx.StaticBox(parent, label=tr(key)), wx.VERTICAL)

    def _build_stackup_page(self, nb):
        page = wx.Panel(nb)
        page.SetBackgroundColour(COL_BG)
        top = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)

        b1 = self._box(page, "grp_diel")
        sb = b1.GetStaticBox()
        self.grid_diel = wx.grid.Grid(sb)
        diel = self._dielectrics()
        self.grid_diel.CreateGrid(len(diel), 4)
        for c, k in enumerate(("col_material", "col_thick", "col_er", "col_tand")):
            self.grid_diel.SetColLabelValue(c, tr(k))
        _style_grid(self.grid_diel)
        self._fill_diel_grid()
        self.grid_diel.SetMinSize((-1, 28 + 26 * max(3, len(diel)) + 4))
        self.grid_diel.Bind(wx.grid.EVT_GRID_CELL_CHANGED, self._on_diel_edit)
        b1.Add(self.grid_diel, 0, wx.EXPAND | wx.ALL, 6)
        prow = wx.BoxSizer(wx.HORIZONTAL)
        prow.Add(wx.StaticText(sb, label=tr("preset")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.choice_preset = wx.Choice(sb, choices=list(PRESETS))
        self.choice_preset.SetSelection(0)
        prow.Add(self.choice_preset, 1, wx.RIGHT, 6)
        btn_preset = wx.Button(sb, label=tr("apply_preset"))
        btn_preset.Bind(wx.EVT_BUTTON, self._on_preset)
        btn_reset = wx.Button(sb, label=tr("board_values"))
        btn_reset.Bind(wx.EVT_BUTTON, self._on_reset_stackup)
        prow.Add(btn_preset, 0, wx.RIGHT, 6)
        prow.Add(btn_reset, 0)
        b1.Add(prow, 0, wx.EXPAND | wx.ALL, 6)
        left.Add(b1, 0, wx.EXPAND | wx.BOTTOM, 8)

        bcu = self._box(page, "grp_cu")
        sbc = bcu.GetStaticBox()
        self.grid_cu = wx.grid.Grid(sbc)
        self._cu_rows = [l for l in self.stackup["layers"] if l["type"] in ("copper", "soldermask")]
        self.grid_cu.CreateGrid(len(self._cu_rows), 4)
        for c, k in enumerate(("col_stack_layer", "col_type", "col_thick_um", "col_er")):
            self.grid_cu.SetColLabelValue(c, tr(k))
        _style_grid(self.grid_cu)
        self._fill_cu_grid()
        self.grid_cu.SetMinSize((-1, 28 + 24 * min(len(self._cu_rows), 5) + 4))
        self.grid_cu.Bind(wx.grid.EVT_GRID_CELL_CHANGED, self._on_cu_edit)
        bcu.Add(self.grid_cu, 0, wx.EXPAND | wx.ALL, 6)
        left.Add(bcu, 0, wx.EXPAND | wx.BOTTOM, 8)

        bus = self._box(page, "grp_user")
        sbu = bus.GetStaticBox()
        urow = wx.BoxSizer(wx.HORIZONTAL)
        self.choice_user = wx.Choice(sbu, choices=[])
        urow.Add(self.choice_user, 1, wx.RIGHT, 6)
        for key, fn in (("u_load", self._on_user_load), ("u_save", self._on_user_save),
                        ("u_delete", self._on_user_delete), ("u_export", self._on_user_export),
                        ("u_import", self._on_user_import)):
            b = wx.Button(sbu, label=tr(key))
            b.Bind(wx.EVT_BUTTON, fn)
            urow.Add(b, 0, wx.RIGHT, 4)
        bus.Add(urow, 0, wx.EXPAND | wx.ALL, 6)
        self._refresh_user_choice()
        left.Add(bus, 0, wx.EXPAND | wx.BOTTOM, 8)

        b2 = self._box(page, "grp_planes")
        sb2 = b2.GetStaticBox()
        self.chk_planes = wx.CheckListBox(sb2, choices=self.copper_ids)
        for i, cid in enumerate(self.copper_ids):
            self.chk_planes.Check(i, cid in self.planes)
        self.chk_planes.SetMinSize((-1, 24 * min(len(self.copper_ids), 4) + 6))
        self.chk_planes.Bind(wx.EVT_CHECKLISTBOX, self._on_planes)
        b2.Add(self.chk_planes, 0, wx.EXPAND | wx.ALL, 6)
        left.Add(b2, 0, wx.EXPAND | wx.BOTTOM, 8)

        b3 = self._box(page, "grp_calc")
        sb3 = b3.GetStaticBox()
        qrow = wx.BoxSizer(wx.HORIZONTAL)
        qrow.Add(wx.StaticText(sb3, label=tr("quality")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.choice_quality = wx.Choice(sb3, choices=[tr("q_fast"), tr("q_std"), tr("q_fine")])
        self.choice_quality.SetSelection(self.quality_idx)
        self.choice_quality.Bind(wx.EVT_CHOICE, self._on_quality)
        qrow.Add(self.choice_quality, 0, wx.RIGHT, 16)
        self.chk_mask = wx.CheckBox(sb3, label=tr("mask"))
        self.chk_mask.SetValue(self.use_mask)
        self.chk_mask.Bind(wx.EVT_CHECKBOX, self._on_mask)
        qrow.Add(self.chk_mask, 0, wx.ALIGN_CENTER_VERTICAL)
        b3.Add(qrow, 0, wx.EXPAND | wx.ALL, 6)
        left.Add(b3, 0, wx.EXPAND | wx.BOTTOM, 8)

        b4 = self._box(page, "grp_models")
        self.txt_models = wx.TextCtrl(b4.GetStaticBox(), style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.txt_models.SetBackgroundColour(wx.WHITE)
        self.txt_models.SetMinSize((-1, 90))
        b4.Add(self.txt_models, 1, wx.EXPAND | wx.ALL, 6)
        left.Add(b4, 1, wx.EXPAND)
        top.Add(left, 3, wx.EXPAND | wx.ALL, 4)

        right = self._box(page, "grp_cross")
        self.pan_stack = StackupPanel(right.GetStaticBox(), self)
        right.Add(self.pan_stack, 1, wx.EXPAND | wx.ALL, 6)
        top.Add(right, 2, wx.EXPAND | wx.ALL, 4)
        page.SetSizer(top)
        return page

    def _on_planes(self, event):
        self.planes = [self.copper_ids[i] for i in range(len(self.copper_ids))
                       if self.chk_planes.IsChecked(i)]
        self._refresh_models()
        self._mark_stale()

    def _on_quality(self, event):
        self.quality_idx = self.choice_quality.GetSelection()
        self._mark_stale()

    def _on_mask(self, event):
        self.use_mask = self.chk_mask.GetValue()
        self._refresh_models()
        self._mark_stale()

    def _fill_diel_grid(self):
        for r, l in enumerate(self._dielectrics()):
            self.grid_diel.SetCellValue(r, 0, l.get("material") or f"dielectric {r+1}")
            self.grid_diel.SetReadOnly(r, 0, True)
            t = (l.get("thicknessNm") or 0) / 1e6
            self.grid_diel.SetCellValue(r, 1, f"{t:.4f}".rstrip("0").rstrip("."))
            self.grid_diel.SetCellValue(r, 2, f"{l.get('epsilonR') or 4.5:.3g}")
            self.grid_diel.SetCellValue(r, 3, f"{l.get('lossTangent') or 0:.4g}")
            for c in (1, 2, 3):
                self.grid_diel.SetCellAlignment(r, c, wx.ALIGN_RIGHT, wx.ALIGN_CENTRE)
        _zebra(self.grid_diel)
        _fit_cols(self.grid_diel, [140, 130, 60, 70])
        if hasattr(self, "grid_cu"):
            self._fill_cu_grid()

    def _on_diel_edit(self, event):
        r, c = event.GetRow(), event.GetCol()
        diel = self._dielectrics()
        if r >= len(diel):
            return
        val = self.grid_diel.GetCellValue(r, c).replace(",", ".").strip()
        try:
            v = float(val)
            if c == 1:
                assert 0.005 <= v <= 10
                diel[r]["thicknessNm"] = int(round(v * 1e6))
            elif c == 3:
                assert 0 <= v <= 0.5
                diel[r]["lossTangent"] = v
            else:
                assert 1.0 <= v <= 20
                diel[r]["epsilonR"] = v
            self._collapse(diel[r])
        except (ValueError, AssertionError):
            wx.Bell()
            self._fill_diel_grid()
            return
        self._refresh_models()
        self._mark_stale()

    # ── eigene Stackups (benannt, JSON in der KiCad-Config) ───────────────

    @staticmethod
    def _user_file():
        base = os.environ.get("Z0_CONFIG_DIR")
        if not base:
            try:
                base = wx.StandardPaths.Get().GetUserConfigDir()
            except Exception:
                base = os.path.expanduser("~")
            base = os.path.join(base, "z0solver")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "user_stackups.json")

    def _user_load_all(self):
        try:
            with open(self._user_file(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _user_store(self, d):
        with open(self._user_file(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)

    def _refresh_user_choice(self, select=None):
        names = sorted(self._user_load_all())
        self.choice_user.Set(names)
        if names:
            self.choice_user.SetSelection(names.index(select) if select in names else 0)

    def _stackup_snapshot(self):
        return {"format": "z0solver-stackup-1",
                "copper": [{"name": l.get("name"), "thicknessNm": l.get("thicknessNm")}
                           for l in self.stackup["layers"] if l["type"] == "copper"],
                "mask": [{"side": l.get("side"), "thicknessNm": l.get("thicknessNm"),
                          "epsilonR": l.get("epsilonR")}
                         for l in self.stackup["layers"] if l["type"] == "soldermask"],
                "dielectrics": [{"material": l.get("material", ""), "thicknessNm": l["thicknessNm"],
                                 "epsilonR": l["epsilonR"], "lossTangent": l.get("lossTangent", 0.0)}
                                for l in self._dielectrics()]}

    def _apply_snapshot(self, snap):
        diel = self._dielectrics()
        cus = [l for l in self.stackup["layers"] if l["type"] == "copper"]
        sd = snap.get("dielectrics", [])
        if len(sd) != len(diel) or len(snap.get("copper", [])) != len(cus):
            wx.MessageBox(tr("user_mismatch", a=len(sd), b=len(diel),
                             c=len(snap.get("copper", [])), d=len(cus)), TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
            return False
        for l, o in zip(diel, sd):
            l["thicknessNm"] = int(o["thicknessNm"])
            l["epsilonR"] = float(o["epsilonR"])
            l["lossTangent"] = float(o.get("lossTangent", 0.0))
            if o.get("material"):
                l["material"] = o["material"]
            self._collapse(l)
        for l, o in zip(cus, snap["copper"]):
            if o.get("thicknessNm"):
                l["thicknessNm"] = int(o["thicknessNm"])
        masks = [l for l in self.stackup["layers"] if l["type"] == "soldermask"]
        for l in masks:
            for o in snap.get("mask", []):
                if o.get("side") == l.get("side") and o.get("thicknessNm"):
                    l["thicknessNm"] = int(o["thicknessNm"])
                    l["epsilonR"] = float(o.get("epsilonR") or 3.3)
        self._fill_diel_grid()
        self._refresh_models()
        self._mark_stale()
        return True

    def _on_user_save(self, event):
        dlg = wx.TextEntryDialog(self, tr("u_name_prompt"), TITLE)
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue().strip():
            return
        name = dlg.GetValue().strip()
        d = self._user_load_all()
        if name in d and wx.MessageBox(tr("u_overwrite", n=name), TITLE, wx.YES_NO, self) != wx.YES:
            return
        d[name] = self._stackup_snapshot()
        try:
            self._user_store(d)
        except OSError as e:
            wx.MessageBox(str(e), TITLE, wx.OK | wx.ICON_ERROR, self)
            return
        self._refresh_user_choice(name)
        self._set_status(tr("u_saved", n=name))

    def _on_user_load(self, event):
        name = self.choice_user.GetStringSelection()
        snap = self._user_load_all().get(name)
        if snap and self._apply_snapshot(snap):
            self._set_status(tr("u_loaded", n=name))

    def _on_user_delete(self, event):
        name = self.choice_user.GetStringSelection()
        d = self._user_load_all()
        if name in d:
            del d[name]
            self._user_store(d)
            self._refresh_user_choice()

    def _on_user_export(self, event):
        dlg = wx.FileDialog(self, tr("u_export"), wildcard="JSON (*.json)|*.json",
                            defaultFile="stackup.json", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        if dlg.ShowModal() != wx.ID_OK:
            return
        with open(dlg.GetPath(), "w", encoding="utf-8") as f:
            json.dump(self._stackup_snapshot(), f, indent=1, ensure_ascii=False)
        self._set_status(tr("u_exported", p=dlg.GetPath()))

    def _on_user_import(self, event):
        dlg = wx.FileDialog(self, tr("u_import"), wildcard="JSON (*.json)|*.json",
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() != wx.ID_OK:
            return
        try:
            with open(dlg.GetPath(), encoding="utf-8") as f:
                snap = json.load(f)
            assert snap.get("format") == "z0solver-stackup-1"
        except (OSError, ValueError, AssertionError):
            wx.MessageBox(tr("u_bad_file"), TITLE, wx.OK | wx.ICON_ERROR, self)
            return
        if self._apply_snapshot(snap):
            name = os.path.splitext(os.path.basename(dlg.GetPath()))[0]
            d = self._user_load_all()
            d[name] = snap
            self._user_store(d)
            self._refresh_user_choice(name)
            self._set_status(tr("u_loaded", n=name))

    def _fill_cu_grid(self):
        g = self.grid_cu
        for r, l in enumerate(self._cu_rows):
            g.SetCellValue(r, 0, l.get("id") or l.get("name") or "")
            g.SetCellValue(r, 1, tr("t_copper") if l["type"] == "copper" else tr("t_mask"))
            g.SetCellValue(r, 2, f"{(l.get('thicknessNm') or 0)/1000:.4g}")
            g.SetCellValue(r, 3, "" if l["type"] == "copper" else f"{l.get('epsilonR') or 3.3:.3g}")
            for c in (0, 1):
                g.SetReadOnly(r, c, True)
            g.SetReadOnly(r, 3, l["type"] == "copper")
            for c in (2, 3):
                g.SetCellAlignment(r, c, wx.ALIGN_RIGHT, wx.ALIGN_CENTRE)
        _zebra(g)
        _fit_cols(g, [100, 100, 140, 70])

    def _on_cu_edit(self, event):
        r, c = event.GetRow(), event.GetCol()
        l = self._cu_rows[r]
        try:
            v = float(self.grid_cu.GetCellValue(r, c).replace(",", ".").strip())
            if c == 2:
                assert 1 <= v <= 500
                l["thicknessNm"] = int(round(v * 1000))
            else:
                assert 1.0 <= v <= 20
                l["epsilonR"] = v
        except (ValueError, AssertionError):
            wx.Bell()
            self._fill_cu_grid()
            return
        self._refresh_models()
        self._mark_stale()

    def _on_preset(self, event):
        vals = PRESETS[self.choice_preset.GetStringSelection()]
        diel = self._dielectrics()
        if len(vals) != len(diel):
            wx.MessageBox(tr("preset_mismatch", a=len(vals), b=len(diel)), TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        for l, (t, er) in zip(diel, vals):
            l["thicknessNm"] = int(round(t * 1e6))
            l["epsilonR"] = er
            self._collapse(l)
        self._fill_diel_grid()
        self._refresh_models()
        self._mark_stale()

    def _on_reset_stackup(self, event):
        for l, o in zip(self.stackup["layers"], self.stackup_orig["layers"]):
            l.update(o)
        self._fill_diel_grid()
        self._refresh_models()
        self._mark_stale()

    def _refresh_models(self):
        self.models = build_models(self.stackup, self.planes)
        lines = []
        for lid, m in self.models.items():
            if m.kind == "microstrip":
                mask = f", {tr('mask_lbl')} {m.mask_t_mm*1000:.0f} µm" if (m.mask_t_mm and self.use_mask) else ""
                lines.append(f"{lid}: {tr('microstrip')}  h={m.h_mm:.4f} mm, εr={m.er1:.2f}, "
                             f"{tr('ref')} {m.ref_below or '-'}{mask}")
            else:
                lines.append(f"{lid}: {tr('stripline')}  h={m.h_mm:.4f}/{m.h2_mm:.4f} mm, "
                             f"εr={m.er1:.2f}/{m.er2:.2f}, "
                             f"{tr('ref')} {m.ref_above or '-'}/{m.ref_below or '-'}")
            for p in m.problems:
                lines.append(f"    ⚠ {tr('warning')}: {p}")
        self.txt_models.SetValue("\n".join(lines))
        self.pan_stack.Refresh()

    # ── Seite 2: Impedanz-Profile ───────────────────────────────────────────

    def _build_profiles_page(self, nb):
        page = wx.Panel(nb)
        page.SetBackgroundColour(COL_BG)
        s = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(page, label=tr("intro2"))
        intro.SetForegroundColour(wx.Colour(60, 60, 60))
        intro.Wrap(900)
        s.Add(intro, 0, wx.ALL, 10)
        self.grid = wx.grid.Grid(page)
        keys = ["col_on", "col_profile", "col_z0", "col_zd", "col_given",
                "col_solve", "col_layer", "col_class"]
        self.grid.CreateGrid(len(self.profiles), len(keys))
        for c, k in enumerate(keys):
            self.grid.SetColLabelValue(c, tr(k))
        _style_grid(self.grid)
        self._fill_profiles_grid()
        self.grid.Bind(wx.grid.EVT_GRID_CELL_CHANGED, lambda e: (self._mark_stale(), e.Skip()))
        s.Add(self.grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        btn_add = wx.Button(page, label=tr("add_profile"))
        btn_del = wx.Button(page, label=tr("del_profile"))
        btn_add.Bind(wx.EVT_BUTTON, self._on_profile_add)
        btn_del.Bind(wx.EVT_BUTTON, self._on_profile_del)
        row.Add(btn_add, 0, wx.RIGHT, 8)
        row.Add(btn_del, 0)
        s.Add(row, 0, wx.ALL, 10)
        page.SetSizer(s)
        return page

    def _mode_labels(self):
        return [tr("mode_gap"), tr("mode_width")]

    def _fill_profiles_grid(self):
        n = len(self.profiles)
        if self.grid.GetNumberRows() < n:
            self.grid.AppendRows(n - self.grid.GetNumberRows())
        for r, p in enumerate(self.profiles):
            self.grid.SetCellEditor(r, 0, wx.grid.GridCellBoolEditor())
            self.grid.SetCellRenderer(r, 0, wx.grid.GridCellBoolRenderer())
            self.grid.SetCellAlignment(r, 0, wx.ALIGN_CENTRE, wx.ALIGN_CENTRE)
            self.grid.SetCellValue(r, 0, "1" if p["on"] else "0")
            self.grid.SetCellValue(r, 1, p["name"])
            self.grid.SetCellValue(r, 2, p["z0"])
            self.grid.SetCellValue(r, 3, p["zdiff"])
            self.grid.SetCellValue(r, 4, p["given"])
            for c in (2, 3, 4):
                self.grid.SetCellAlignment(r, c, wx.ALIGN_RIGHT, wx.ALIGN_CENTRE)
            self.grid.SetCellEditor(r, 5, wx.grid.GridCellChoiceEditor(self._mode_labels()))
            self.grid.SetCellValue(r, 5, tr("mode_gap") if p["mode"] == "gap" else tr("mode_width"))
            self.grid.SetCellEditor(r, 6, wx.grid.GridCellChoiceEditor(self.copper_ids))
            self.grid.SetCellValue(r, 6, p["layer"])
            self.grid.SetCellEditor(r, 7, wx.grid.GridCellChoiceEditor(["-"] + self.class_names))
            self.grid.SetCellValue(r, 7, p["cls"])
        _zebra(self.grid)
        _fit_cols(self.grid, [70, 130, 100, 110, 110, 170, 120, 150], stretch_col=7)

    def _on_profile_add(self, event):
        self._read_profiles()
        self.profiles.append({"on": True, "name": f"PROFILE{len(self.profiles)+1}",
                              "z0": "50", "zdiff": "", "given": "0.25",
                              "mode": "gap", "layer": self.copper_ids[0], "cls": "-"})
        self._fill_profiles_grid()
        self._mark_stale()

    def _on_profile_del(self, event):
        if len(self.profiles) > 1:
            self.profiles.pop()
            self.grid.DeleteRows(self.grid.GetNumberRows() - 1, 1)
            self._fill_profiles_grid()
            self._mark_stale()

    def _read_profiles(self):
        problems = []
        for r, p in enumerate(self.profiles):
            if r >= self.grid.GetNumberRows():
                break
            p["on"] = self.grid.GetCellValue(r, 0) in ("1", True, 1)
            p["name"] = self.grid.GetCellValue(r, 1).strip() or f"PROFILE{r+1}"
            p["z0"] = self.grid.GetCellValue(r, 2).strip()
            p["zdiff"] = self.grid.GetCellValue(r, 3).strip()
            p["given"] = self.grid.GetCellValue(r, 4).strip() or "0.25"
            p["mode"] = "width" if self.grid.GetCellValue(r, 5) == tr("mode_width") else "gap"
            p["layer"] = self.grid.GetCellValue(r, 6) or self.copper_ids[0]
            p["cls"] = self.grid.GetCellValue(r, 7) or "-"
            if not p["on"]:
                continue
            def num(txt, lo, hi):
                try:
                    v = float(txt.replace(",", "."))
                    return lo <= v <= hi
                except ValueError:
                    return False
            if p["z0"] and not num(p["z0"], 5, 300):
                problems.append(tr("e_z0", n=p["name"], v=p["z0"]))
            if p["zdiff"]:
                if not num(p["zdiff"], 20, 400):
                    problems.append(tr("e_zd", n=p["name"], v=p["zdiff"]))
                if not num(p["given"], 0.02, 5):
                    problems.append(tr("e_given", n=p["name"], v=p["given"]))
            if not p["z0"] and not p["zdiff"]:
                problems.append(tr("e_notarget", n=p["name"]))
        return problems

    # ── Seite 3: Ergebnisse ─────────────────────────────────────────────────

    def _build_results_page(self, nb):
        page = wx.Panel(nb)
        page.SetBackgroundColour(COL_BG)
        s = wx.BoxSizer(wx.VERTICAL)
        keys = ["col_profile", "col_res_layer", "col_res_model", "col_res_w", "col_res_g",
                "col_z0", "col_zd", "col_res_status"]
        self.grid_res = wx.grid.Grid(page)
        self.grid_res.CreateGrid(0, len(keys))
        for c, k in enumerate(keys):
            self.grid_res.SetColLabelValue(c, tr(k))
        _style_grid(self.grid_res)
        self.grid_res.Bind(wx.grid.EVT_GRID_SELECT_CELL, self._on_result_sel)
        s.Add(self.grid_res, 3, wx.EXPAND | wx.ALL, 8)
        box = wx.StaticBoxSizer(wx.StaticBox(page, label=tr("grp_cross")), wx.VERTICAL)
        self.pan_result = ResultPanel(box.GetStaticBox(), self)
        box.Add(self.pan_result, 1, wx.EXPAND | wx.ALL, 4)
        brow = wx.BoxSizer(wx.HORIZONTAL)
        btn_curve = wx.Button(box.GetStaticBox(), label=tr("curve_btn"))
        btn_curve.Bind(wx.EVT_BUTTON, self._on_curve)
        brow.Add(btn_curve, 0)
        box.Add(brow, 0, wx.ALL, 4)
        s.Add(box, 3, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.txt_results = wx.StaticText(page, label="")
        s.Add(self.txt_results, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        page.SetSizer(s)
        return page

    def _on_result_sel(self, event):
        r = event.GetRow()
        if 0 <= r < len(self.results):
            self.sel_result = self.results[r]
            self.sel_layer = self.sel_result["layer"]
            self.pan_result.curve = []
        self.pan_result.Refresh()
        self.pan_stack.Refresh()
        event.Skip()

    def _on_curve(self, event):
        r = self.sel_result
        if not r or not r.get("width_mm"):
            wx.MessageBox(tr("need_result"), TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        m = self.models.get(r["layer"])
        if not m:
            return
        from fieldsolver import solve as _solve
        w0 = r["width_mm"]
        target = r.get("zdiff_target") if r.get("gap_mm") else r.get("z0_target")
        pts = []
        with wx.BusyCursor():
            for f in (0.5, 0.7, 0.85, 1.0, 1.2, 1.5):
                try:
                    st = structure_from_model(m, w0 * f, r.get("gap_mm"), self.use_mask, "schnell")
                    res = _solve(st)
                    pts.append((w0 * f, res.zdiff if r.get("gap_mm") else res.z0))
                except Exception:
                    pass
        self.pan_result.curve = pts
        self.pan_result.curve_target = target
        self.pan_result.Refresh()

    # ── Berechnen ───────────────────────────────────────────────────────────

    def _quality(self):
        return ("schnell", "standard", "fein")[self.quality_idx]

    def on_run(self, event):
        problems = self._read_profiles()
        if problems:
            self.nb.SetSelection(1)
            wx.MessageBox("\n".join(problems), TITLE, wx.OK | wx.ICON_ERROR, self)
            return
        todo = [p for p in self.profiles if p["on"]]
        if not todo:
            wx.MessageBox(tr("e_noprofile"), TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        quality = self._quality()
        use_mask = self.use_mask
        self.results = []
        n = len(todo) * len(self.models)
        self.gauge.SetRange(max(1, n))
        self.gauge.SetValue(0)
        self.gauge.Show()
        self.btn_run.Enable(False)
        self.Layout()
        step = 0
        try:
            for p in todo:
                z0t = float(p["z0"].replace(",", ".")) if p["z0"] else None
                zdt = float(p["zdiff"].replace(",", ".")) if p["zdiff"] else None
                given = float(p["given"].replace(",", ".")) if zdt else None
                for lid, m in self.models.items():
                    step += 1
                    self._set_status(tr("st_running", p=p["name"], l=lid, i=step, n=n))
                    self.gauge.SetValue(step - 1)
                    wx.GetApp().Yield() if wx.GetApp() else None
                    row = {"profile": p["name"], "netclass": p["name"],
                           "layer": lid, "model_kind": m.kind,
                           "width_mm": None, "gap_mm": None,
                           "z0": None, "zdiff": None, "error": None,
                           "z0_target": z0t, "zdiff_target": zdt}
                    try:
                        if m.problems:
                            raise SolveError("; ".join(m.problems))
                        if z0t:
                            final, res = solve_width(
                                structure_from_model(m, 0.2, None, use_mask, quality),
                                z0t, quality=quality)
                            w = final.w / MM
                            row.update(width_mm=w, z0=res.z0, z0_result=res, track_width_mm=w)
                        if zdt:
                            if p["mode"] == "gap":
                                base = structure_from_model(m, given, given, use_mask, quality)
                                final, res = solve_gap(base, zdt, quality=quality)
                                row.update(width_mm=given, gap_mm=final.gap / MM,
                                           dp_width_mm=given, dp_gap_mm=final.gap / MM)
                            else:
                                base = structure_from_model(m, 0.2, given, use_mask, quality)
                                final, res = solve_width_for_zdiff(base, zdt, quality=quality)
                                row.update(width_mm=final.w / MM, gap_mm=given,
                                           dp_width_mm=final.w / MM, dp_gap_mm=given)
                            row.update(zdiff=res.zdiff, zdiff_result=res)
                    except SolveError as e:
                        if e.target is not None:
                            row["error"] = tr("e_unreach", t=e.target, b=e.best, x=e.at_mm)
                        else:
                            row["error"] = str(e)
                    except Exception as e:
                        row["error"] = str(e)
                    self.results.append(row)
        finally:
            self.gauge.SetValue(n)
            self.gauge.Hide()
            self.btn_run.Enable(True)
        self.stale = False
        self._update_banner()
        self._show_results()
        self._build_proposals()
        for b in (self.btn_apply, self.btn_dru, self.btn_report, self.btn_tune, self.btn_check):
            b.Enable(True)
        ok = [x for x in self.results if not x["error"]]
        self._set_status(tr("st_done", ok=len(ok), n=len(self.results)))
        first = next((x for x in self.results if not x["error"]), None) or (self.results[0] if self.results else None)
        if first:
            self.sel_result, self.sel_layer = first, first["layer"]
            self.pan_result.curve = []
        self.nb.SetSelection(2)
        self.pan_result.Refresh()
        self.pan_stack.Refresh()

    def _show_results(self):
        g = self.grid_res
        if g.GetNumberRows():
            g.DeleteRows(0, g.GetNumberRows())
        g.AppendRows(len(self.results))
        dash = "–"
        for r, row in enumerate(self.results):
            kind = tr("microstrip") if row["model_kind"] == "microstrip" else tr("stripline")
            status = tr("status_err") if row["error"] else tr("status_ok")
            vals = [row["profile"], row["layer"], kind,
                    dash if row["width_mm"] is None else f"{row['width_mm']:.4f}",
                    dash if row["gap_mm"] is None else f"{row['gap_mm']:.4f}",
                    dash if row["z0"] is None else f"{row['z0']:.2f}",
                    dash if row["zdiff"] is None else f"{row['zdiff']:.2f}",
                    status]
            for c, v in enumerate(vals):
                g.SetCellValue(r, c, v)
                g.SetReadOnly(r, c, True)
                if c >= 3 and c <= 6:
                    g.SetCellAlignment(r, c, wx.ALIGN_RIGHT, wx.ALIGN_CENTRE)
                g.SetCellBackgroundColour(r, c, COL_ERR if row["error"] else (COL_ROW_ALT if r % 2 else wx.WHITE))
            if row["error"]:
                g.SetCellTextColour(r, 7, wx.Colour(170, 30, 30))
            else:
                g.SetCellTextColour(r, 7, wx.Colour(30, 120, 50))
        _fit_cols(g, [110, 90, 110, 100, 100, 90, 100, 90], stretch_col=7)
        bad = [x for x in self.results if x["error"]]
        self.txt_results.SetLabel("\n".join(f"{x['profile']} / {x['layer']}: {x['error']}" for x in bad[:3]))
        try:
            self.txt_results.Wrap(max(300, self.GetClientSize().width - 40))
            self.Layout()
        except Exception:
            pass
        self.page_results.Layout()

    def _build_proposals(self):
        self.proposals = {}
        for p in self.profiles:
            if not p["on"] or p["cls"] == "-":
                continue
            row = next((x for x in self.results
                        if x["profile"] == p["name"] and x["layer"] == p["layer"]
                        and not x["error"]), None)
            if not row:
                continue
            fields: Dict[str, int] = {}
            if row.get("z0_target"):
                fields["track_width"] = mm_to_nm(row["width_mm"])
            if row.get("zdiff_target"):
                fields["diff_pair_width"] = mm_to_nm(row["width_mm"])
                fields["diff_pair_gap"] = mm_to_nm(row["gap_mm"])
            self.proposals[p["cls"]] = fields

    # ── Anwenden / Regeln / Bericht ─────────────────────────────────────────

    def on_apply(self, event):
        if not self.proposals:
            wx.MessageBox(tr("e_noprop"), TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        blocked = write_blocked_reason(kicad_version(self.kicad))
        current = read_classes(self.project)
        preview = make_preview(current, self.proposals)
        if blocked:
            wx.MessageBox(blocked + "\n\n" + preview.manual_text(), TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        if preview.empty:
            wx.MessageBox(tr("all_set"), TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        rc = wx.MessageBox(tr("confirm_write") + "\n\n" + preview.text(),
                           TITLE, wx.YES_NO | wx.ICON_QUESTION, self)
        if rc != wx.YES:
            return
        ok, msg = apply(self.project, preview, current)
        if ok:
            wx.MessageBox(tr("written_ok") + ("\n" + msg if msg else ""), TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
        else:
            wx.MessageBox(tr("written_fail") + "\n" + msg, TITLE, wx.OK | wx.ICON_ERROR, self)

    def _layer_rules(self):
        out = []
        for p in self.profiles:
            if not p["on"] or p["cls"] == "-":
                continue
            for x in self.results:
                if x["profile"] != p["name"] or x["error"]:
                    continue
                fields = {}
                if x.get("z0_target") and x["width_mm"]:
                    fields["track_width"] = mm_to_nm(x["width_mm"])
                if x.get("zdiff_target") and x["gap_mm"]:
                    fields["diff_pair_gap"] = mm_to_nm(x["gap_mm"])
                    fields["track_width"] = mm_to_nm(x["width_mm"])
                if fields:
                    out.append((p["cls"], x["layer"], fields))
        return out

    def on_dru(self, event):
        if not self.results:
            return
        text = build_dru(self.proposals, layer_rules=self._layer_rules())
        default = os.path.splitext(getattr(self.board, "file_name", "board"))[0] + ".kicad_dru"
        with wx.FileDialog(self, tr("save_dru"), wildcard="*.kicad_dru",
                           defaultFile=os.path.basename(default),
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                with open(dlg.GetPath(), "w", encoding="utf-8") as fh:
                    fh.write(text)
                wx.MessageBox(tr("written_to", p=dlg.GetPath()), TITLE,
                              wx.OK | wx.ICON_INFORMATION, self)

    def tune_table(self):
        """Tabelle im Aufbau des KiCad-10-Tuning-Profils (Signal Layer, Bezuege, Breite, Gap)."""
        hdr = ["Profile", "Signal layer", "Bottom ref", "Top ref", "Track width (mm)",
               "Diff pair gap (mm)", "Z0 (ohm)", "Zdiff (ohm)"]
        rows = ["\t".join(hdr)]
        for r in self.results:
            if r["error"]:
                continue
            m = self.models.get(r["layer"])
            top = (m.ref_above if m and m.kind == "stripline" else "") or ""
            bot = (m.ref_below if m else "") or ""
            z0 = r.get("z0_result"); zd = r.get("zdiff_result")
            rows.append("\t".join([
                r["profile"], r["layer"], bot, top, f"{r['width_mm']:.4f}",
                f"{r['gap_mm']:.4f}" if r.get("zdiff_target") else "",
                f"{z0.z0:.2f}" if z0 is not None else "",
                f"{zd.zdiff:.2f}" if zd is not None else ""]))
        return "\n".join(rows)

    def on_tune(self, event):
        txt = self.tune_table()
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(txt))
            wx.TheClipboard.Close()
        self._set_status(tr("tune_copied"))
        wx.MessageBox(tr("tune_howto") + "\n\n" + txt, TITLE, wx.OK | wx.ICON_INFORMATION, self)

    def _check_targets(self):
        tg = {}
        for p in self.profiles:
            if not p["on"] or p["cls"] == "-":
                continue
            row = next((x for x in self.results if x["profile"] == p["name"]
                        and not x["error"]), None)
            for x in self.results:
                if x["profile"] == p["name"] and not x["error"]:
                    tg[(p["cls"], x["layer"])] = (mm_to_nm(x["width_mm"]), 0.05)
        return tg

    def on_check(self, event):
        try:
            items = collect_tracks(self.board)
        except Exception as e:  # keine API / altes KiCad
            wx.MessageBox(tr("chk_fail", e=str(e)), TITLE, wx.OK | wx.ICON_WARNING, self)
            return
        rows = check_tracks(items, self._check_targets())
        lines = format_rows(rows, tr) or [tr("chk_none")]
        bad = sum(r["bad"] for r in rows)
        wx.MessageBox("\n".join(lines) + "\n\n" + tr("chk_note"), TITLE,
                      wx.OK | (wx.ICON_WARNING if bad else wx.ICON_INFORMATION), self)
        self._set_status(tr("chk_summary", bad=bad, n=len(items)))

    def on_report(self, event):
        if not self.results:
            return
        html_text = build_html(
            "Z0 Field Solver", describe_stackup(self.stackup), self.models, self.results,
            notes=["2D finite-difference field solver (SOR)."])
        with wx.FileDialog(self, tr("save_html"), wildcard="*.html",
                           defaultFile="z0-report.html",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                with open(dlg.GetPath(), "w", encoding="utf-8") as fh:
                    fh.write(html_text)
                wx.MessageBox(tr("written_to", p=dlg.GetPath()), TITLE,
                              wx.OK | wx.ICON_INFORMATION, self)
