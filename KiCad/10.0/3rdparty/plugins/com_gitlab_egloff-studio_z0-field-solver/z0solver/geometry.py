# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""Geometrie loesen: Breite bzw. Gap fuer eine Zielimpedanz finden.

Strategie: Bisektion auf grobem Gitter (schnell), dann wenige Schritte auf
der Ziel-Qualitaet und eine finale Kontrollrechnung. So dauert eine Loesung
typischerweise 10-30 s statt Minuten.

Monotonie (fuer die Bisektion genutzt):
  Z0 faellt mit steigender Breite;  Zdiff steigt mit wachsendem Gap.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Optional, Tuple

from fieldsolver import Structure, SolveResult, solve

UM = 1e-6
MM = 1e-3


class SolveError(Exception):
    """Ziel nicht erreichbar (ausserhalb des physikalisch sinnvollen Bereichs)."""

    def __init__(self, msg, target=None, best=None, at_mm=None):
        super().__init__(msg)
        self.target, self.best, self.at_mm = target, best, at_mm


def _at(s: Structure, **kw) -> Structure:
    return dataclasses.replace(s, **kw)


def _bisect(func, lo, hi, target, tol, max_steps):
    """func monoton; gibt (x, y) mit |y - target| <= tol oder bestem Wert."""
    f_lo, f_hi = func(lo), func(hi)
    # sicherstellen: lo < hi und Ziel zwischen den Endwerten
    if (f_lo - target) * (f_hi - target) > 0:
        raise SolveError(
            f"Ziel {target:.2f} Ohm ausserhalb des erreichbaren Bereichs "
            f"({min(f_lo, f_hi):.2f} bis {max(f_lo, f_hi):.2f} Ohm)."
        )
    best_x, best_y = (lo, f_lo) if abs(f_lo - target) <= abs(f_hi - target) else (hi, f_hi)
    for _ in range(max_steps):
        if abs(best_y - target) <= tol:
            break
        mid = 0.5 * (lo + hi)
        f_mid = func(mid)
        if abs(f_mid - target) < abs(best_y - target):
            best_x, best_y = mid, f_mid
        if (f_lo - target) * (f_mid - target) <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return best_x, best_y


def _secant(func, x0, target, tol, max_steps, lo, hi):
    """Nullstelle von func(x)-target (monoton) mit Sekante im Log-Raum plus
    Klammer (Bracket): ausserhalb der Klammer wird geometrisch halbiert.
    Meist 4-7 Auswertungen statt 15-20. Gibt (x, y)."""
    import math as _m
    x0 = min(max(x0, lo), hi)
    pts = [(x0, func(x0))]
    best = pts[0]
    lo_pt = hi_pt = None          # Klammer: Punkte mit y<target bzw. y>target
    def note(p):
        nonlocal lo_pt, hi_pt, best
        if abs(p[1] - target) < abs(best[1] - target):
            best = p
        if p[1] < target:
            if lo_pt is None or (hi_pt is not None and False) or True:
                lo_pt = p if (lo_pt is None or abs(p[1]-target) < abs(lo_pt[1]-target)) else lo_pt
        else:
            hi_pt = p if (hi_pt is None or abs(p[1]-target) < abs(hi_pt[1]-target)) else hi_pt
    note(pts[0])
    for _ in range(max_steps):
        if abs(best[1] - target) <= tol:
            return best
        if lo_pt is None or hi_pt is None:
            # noch nicht eingeklammert: in Richtung Ziel ausweiten
            xl = pts[-1][0]
            yl = pts[-1][1]
            if len(pts) >= 2 and pts[-1][1] != pts[-2][1]:
                (xa, ya), (xb, yb) = pts[-2], pts[-1]
                ln = _m.log(xb) + (target - yb) * (_m.log(xb) - _m.log(xa)) / (yb - ya)
                xn = _m.exp(min(max(ln, _m.log(lo)), _m.log(hi)))
                xn = min(max(xn, xb / 3.0), xb * 3.0)
            else:
                xn = xl * 1.25
            xn = min(max(xn, lo), hi)
            if xn == xl:
                break
        else:
            xa, ya = lo_pt
            xb, yb = hi_pt
            dy = (yb - ya) or 1e-12
            ln = _m.log(xa) + (target - ya) * (_m.log(xb) - _m.log(xa)) / dy
            xn = _m.exp(min(max(ln, _m.log(lo)), _m.log(hi)))
            lo_x, hi_x = sorted((xa, xb))
            if not (lo_x * 1.0001 < xn < hi_x / 1.0001):
                xn = _m.sqrt(xa * xb)
        p = (xn, func(xn))
        pts.append(p)
        note(p)
    if abs(best[1] - target) > max(tol * 4, 1.0):
        raise SolveError(
            f"Ziel {target:.2f} Ohm nicht erreichbar (naechster Wert {best[1]:.2f} Ohm "
            f"bei {best[0]*1e3:.3f} mm).", target=target, best=best[1], at_mm=best[0] * 1e3)
    return best


def solve_width(s: Structure, target_z: float, quality: str = "standard",
                tol_ohm: float = 0.25) -> Tuple[Structure, SolveResult]:
    """Breite fuer Ziel-Z0 (single-ended) finden. Gibt (Struktur, Ergebnis)."""
    lo, hi = 30 * UM, 4.0 * MM
    coarse = _at(s, gap=None, quality="schnell")
    w0, _ = _secant(lambda w: solve(_at(coarse, w=w)).z0, s.w, target_z, 1.0, 10, lo, hi)
    fine = _at(s, gap=None, quality=quality)
    w1, _ = _secant(lambda w: solve(_at(fine, w=w)).z0, w0, target_z, tol_ohm, 4, lo, hi)
    final = _at(fine, w=w1)
    return final, solve(final)


def solve_gap(s: Structure, target_zdiff: float, quality: str = "standard",
              tol_ohm: float = 0.5) -> Tuple[Structure, SolveResult]:
    """Gap fuer Ziel-Zdiff bei gegebener Breite finden."""
    lo = max(20 * UM, 0.5 * s.t)
    hi = 6.0 * s.h + 2.0 * s.w
    coarse = _at(s, quality="schnell", gap=s.gap or max(lo, s.w))
    g0, _ = _secant(lambda g: solve(_at(coarse, gap=g)).zdiff, coarse.gap, target_zdiff, 1.0, 10, lo, hi)
    fine = _at(s, quality=quality, gap=g0)
    g1, _ = _secant(lambda g: solve(_at(fine, gap=g)).zdiff, g0, target_zdiff, tol_ohm, 4, lo, hi)
    final = _at(fine, gap=g1)
    return final, solve(final)


def solve_width_for_zdiff(s: Structure, target_zdiff: float, quality: str = "standard",
                          tol_ohm: float = 0.5) -> Tuple[Structure, SolveResult]:
    """Breite fuer Ziel-Zdiff bei festem Gap finden."""
    if not s.gap:
        raise SolveError("Fuer diese Loesung muss ein Gap vorgegeben sein.")
    lo, hi = 30 * UM, 3.0 * MM
    coarse = _at(s, quality="schnell")
    w0, _ = _secant(lambda w: solve(_at(coarse, w=w)).zdiff, s.w, target_zdiff, 1.0, 10, lo, hi)
    fine = _at(s, quality=quality)
    w1, _ = _secant(lambda w: solve(_at(fine, w=w)).zdiff, w0, target_zdiff, tol_ohm, 4, lo, hi)
    final = _at(fine, w=w1)
    return final, solve(final)
