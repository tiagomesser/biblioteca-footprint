# Copyright (C) 2026 Egloff Studio
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Z0 Field Solver. See LICENSE.
"""2D-Feldlöser für PCB-Querschnitte ( elektrostatisch, Laplace-Gleichung ).

Physik (wie bei jankae/RF2DFieldSolver, hier eigenstaendig in NumPy):
  1. Potentialfeld phi aus  div(eps grad phi) = 0  im Querschnitt loesen
     (Finite Differenzen, Rot-Schwarz-SOR, Leiter als Dirichlet-Rand).
  2. Ladung auf dem Signal-Leiter ueber das Gauss-Integral  Q = oint eps0 epsr dphi/dn dl.
  3. Zwei Laeufe: mit Dielektrikum (C) und mit Luft (C0).
     Daraus  Z0 = 1 / (c * sqrt(C * C0))   und   eps_eff = C / C0.
  4. Differentiell: P auf +1 V, N auf -1 V (Odd-Mode).
     C_odd = Q_P / 1 V,  Zdiff = 2 * Z_odd = 2 / (c * sqrt(C_odd * C_odd0)).

Alles in SI-Einheiten (Meter, Farad/Meter, Ohm). Reines NumPy, keine
weiteren Abhaengigkeiten. Python-3.9-kompatibel.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

C0 = 299792458.0        # Lichtgeschwindigkeit m/s
EPS0 = 8.8541878128e-12  # elektrische Feldkonstante F/m

UM = 1e-6
MM = 1e-3


@dataclass
class Structure:
    """Ein zu loesender Leiterbahn-Querschnitt (SI-Einheiten)."""
    kind: str                 # "microstrip" | "stripline" | "cpw"
    w: float                  # Leiterbahnbreite
    t: float                  # Kupferdicke
    h: float                  # Dielektrikum: Leiter -> untere/naechste Bezugslage
    er1: float                # eps_r des Dielektrikums unter dem Leiter
    h2: float = 0.0           # stripline: Leiter -> obere Bezugslage (0 = wie h)
    er2: float = 0.0          # stripline: eps_r oben (0 = wie er1)
    gap: Optional[float] = None      # differentiell: Innenabstand der Paare
    mask_t: float = 0.0       # Loetstopplack-Dicke (nur microstrip, 0 = aus)
    mask_er: float = 3.3      # eps_r Loetstopplack
    # Rechenparameter
    margin_x: float = 0.0     # zusaetzlicher Luft-/Materialrand seitlich (0 = auto)
    margin_top: float = 0.0   # Luft ueber dem Aufbau bei microstrip (0 = auto)
    cell: float = 0.0         # Zellgroesse (0 = auto, siehe quality)
    quality: str = "standard" # "schnell" | "standard" | "fein"
    tol: float = 1e-6         # Konvergenz: max. Potentialaenderung
    max_iter: int = 40000


@dataclass
class SolveResult:
    z0: float                       # single-ended Impedanz (Ohm)
    eps_eff: float
    capacitance: float              # F/m mit Dielektrikum
    capacitance_air: float          # F/m in Luft
    zdiff: Optional[float] = None   # differentielle Impedanz (Ohm)
    eps_eff_odd: Optional[float] = None
    c_odd: Optional[float] = None
    c_odd_air: Optional[float] = None
    iterations: int = 0
    converged: bool = True
    grid: tuple = (0, 0)            # (Zeilen, Spalten)
    cell: float = 0.0


#: Feinheitsfaktor je Qualitaetsstufe (1.0 = grob, kleiner = feiner).
_QUALITY_SCALE = {"schnell": 1.0, "standard": 0.6, "fein": 0.35}


def _auto_cell(s: Structure) -> float:
    """Zellgroesse: fein genug fuer Kupferdicke, Breite, Hoehe und Gap.

    Grob (Faktor 1.0) hat ~5% Gitterfehler, Standard (0.6) <1%,
    fein (0.35) <0,3% - gemessen an der Gitterkonvergenz.
    """
    cands = [s.t / 2.0, s.w / 8.0, s.h / 6.0]
    if s.kind == "cpw":
        # Kupferdicke nur 1 Zelle: der Dicken-Effekt ist bei CPW klein,
        # ein 2-Zellen-Raster sprengt sonst die Domain-Groesse.
        cands = [s.t, s.w / 8.0, (s.gap or s.w) / 4.0]
    if s.kind == "stripline":
        h2 = s.h2 if s.h2 > 0 else s.h
        cands.append(h2 / 6.0)
    if s.gap:
        cands.append(s.gap / 4.0)
    cell = min(c for c in cands if c > 0)
    scale = _QUALITY_SCALE.get(s.quality, 0.6)
    cell = max(cell * scale, 0.25 * UM)
    return cell  # _snap_cell(s, cell) getestet: MS besser, SL schlechter -> aus (siehe TODO)


def _snap_cell(s: Structure, cell: float) -> float:
    """Zellgroesse so waehlen (0.72..1.0 x), dass Breite, Hoehen, Dicke und Gap
    moeglichst genau ganzzahlige Vielfache sind. Ohne das springt Z0 mit der
    Gitterfeinheit um +-2-5 % (Rundung der Geometrie auf das Raster)."""
    dims = [d for d in (s.w, s.h, s.h2 if s.h2 > 0 else 0.0, s.t, s.gap or 0.0) if d > 0]
    best, best_err = cell, 9.9
    for k in range(0, 121):
        c = cell * (1.0 - 0.28 * k / 120.0)
        err = 0.0
        for d in dims:
            n = d / c
            err = max(err, abs(n - max(1, round(n))) / n)
        # kleine Strafe fuer feinere Zellen (Rechenzeit)
        score = err + 0.02 * (1.0 - c / cell)
        if score < best_err - 1e-12:
            best, best_err = c, score
    return best


def _build_grid(s: Structure, air_only: bool):
    """Materialraster, Leiterpotentiale und fixe Zellen aufbauen.

    Rueckgabe: (eps, fixed, value, ny, nx, cell, conductor_masks)
      eps   : (ny, nx) float, relative Permittivitaet je Zelle
      fixed : (ny, nx) bool, Dirichlet-Zellen (Leiter + Rand)
      value : (ny, nx) float, Potential der fixen Zellen
      conductor_masks: Liste von bool-Arrays, eine je Signal-Leitergruppe
    """
    cell = s.cell or _auto_cell(s)
    h2 = s.h2 if s.h2 > 0 else s.h
    er2 = s.er2 if s.er2 > 0 else s.er1

    # Domain-Ausdehnung
    pair_span = (s.w + (s.gap or 0.0)) if s.gap else s.w
    if s.kind == "cpw":
        if not s.gap:
            raise ValueError("cpw braucht gap (Schlitzbreite).")
        gw = 3.0 * (s.w + 2.0 * s.gap)          # Breite der Massestreifen
        margin_x = s.margin_x or 1.0 * (s.w + 2.0 * s.gap)
        width = s.w + 2.0 * s.gap + 2.0 * gw + 2.0 * margin_x
        top_air = s.margin_top or 3.0 * (s.w + 2.0 * s.gap)
        air_below = 3.0 * (s.w + 2.0 * s.gap)   # klassisches CPW: Luft unter dem Substrat
        height = air_below + s.h + s.t + top_air
    else:
        if s.kind == "stripline":
            # Zwischen zwei Planes klingt das Feld seitlich wie exp(-pi x/b), b=h+h2:
            # 3b Rand -> Rest < 1e-4, ein groesserer Rand kostet nur Rechenzeit.
            margin_x = s.margin_x or max(3.0 * (s.h + h2), 3.0 * pair_span)
        else:
            margin_x = s.margin_x or max(9.0 * s.h, 5.0 * pair_span, 9.0 * h2)
        width = pair_span + 2.0 * margin_x
        if s.kind == "microstrip":
            top_air = s.margin_top or max(9.0 * s.h, 14.0 * s.t, 0.7 * MM)
            height = s.h + s.t + top_air
        else:
            height = s.h + s.t + h2

    if not s.cell:
        cell = max(cell, width / 1800.0)       # Rechenzeit begrenzen (Dicke min. 1 Zelle)
    nx = max(24, int(math.ceil(width / cell)) + 1)
    ny = max(24, int(math.ceil(height / cell)) + 1)

    eps = np.ones((ny, nx), dtype=np.float64)
    if not air_only:
        if s.kind == "cpw":
            y_a = int(round(air_below / cell))
            y_b = int(round((air_below + s.h) / cell))
            eps[y_a:y_b, :] = s.er1
        elif s.kind == "microstrip":
            y_sub = int(round(s.h / cell))          # Dielektrikum 0..h
            eps[0:y_sub, :] = s.er1
            if s.mask_t > 0:
                y0 = int(round((s.h + s.t) / cell))
                y1 = min(ny - 1, int(round((s.h + s.t + s.mask_t) / cell)) + 1)
                eps[y0:y1, :] = s.mask_er
        else:
            y_b = int(round(s.h / cell))
            y_t = min(ny - 1, int(round((s.h + s.t + h2) / cell)) + 1)
            eps[0:y_b, :] = s.er1
            eps[y_b:y_t, :] = er2

    fixed = np.zeros((ny, nx), dtype=bool)
    value = np.zeros((ny, nx), dtype=np.float64)

    # Aeussere Berandung: fernes Bezugspotential (0 V)
    fixed[0, :] = True
    fixed[-1, :] = True
    fixed[:, 0] = True
    fixed[:, -1] = True

    # Bezugslagen (volle Breite, 0 V); cpw hat keine - Masse liegt neben dem Leiter
    if s.kind == "microstrip":
        fixed[0, :] = True                      # untere Lage = GND-Plane
    elif s.kind == "stripline":
        y_top_plane = min(ny - 1, int(round((s.h + s.t + h2) / cell)))
        fixed[y_top_plane, :] = True            # obere Plane

    # Signal-Leiter (rechteckig, trapezfoermige Aetzung wird vernachlaessigt)
    conductor_masks = []
    y_trace = s.h if s.kind != "cpw" else air_below + s.h
    y0 = int(round(y_trace / cell))
    y1 = max(y0 + 1, int(round((y_trace + s.t) / cell)) + 1)
    x_c = nx // 2
    hw = max(1, int(round((s.w / 2.0) / cell)))
    if s.kind == "cpw":
        slot = max(1, int(round(s.gap / cell)))
        gwl = int(round(gw / cell))
        trace_mask = np.zeros((ny, nx), dtype=bool)
        trace_mask[y0:y1, x_c - hw:x_c + hw + 1] = True
        gnd_mask = np.zeros((ny, nx), dtype=bool)
        xl = x_c - hw - slot
        xr = x_c + hw + slot
        gnd_mask[y0:y1, max(1, xl - gwl):xl + 1] = True
        gnd_mask[y0:y1, xr:min(nx - 1, xr + gwl + 1)] = True
        fixed |= trace_mask | gnd_mask
        value[trace_mask] = 1.0
        value[gnd_mask] = 0.0
        conductor_masks.append(trace_mask)
        return eps, fixed, value, ny, nx, cell, conductor_masks
    traces = []
    if s.gap:
        half_pitch = (s.gap / 2.0 + s.w / 2.0) / cell
        xc_p = int(round(x_c - half_pitch))
        xc_n = int(round(x_c + half_pitch))
        traces = [(xc_p - hw, xc_p + hw, +1.0), (xc_n - hw, xc_n + hw, -1.0)]
    else:
        traces = [(x_c - hw, x_c + hw, +1.0)]
    for xa, xb, v in traces:
        xa = max(1, xa)
        xb = min(nx - 2, xb)
        mask = np.zeros((ny, nx), dtype=bool)
        mask[y0:y1, xa:xb + 1] = True
        fixed |= mask
        value[mask] = v
        conductor_masks.append(mask)

    return eps, fixed, value, ny, nx, cell, conductor_masks


def _solve_phi_sparse(eps, fixed, value, coo_matrix, spsolve):
    """Gleiches Problem direkt (duenn besetzte LU) loesen - Sekunden statt Minuten."""
    ny, nx = eps.shape
    unk = ~fixed
    n = int(unk.sum())
    idx = -np.ones((ny, nx), dtype=np.int64)
    idx[unk] = np.arange(n)
    ys, xs = np.nonzero(unk)
    me = idx[ys, xs]
    rows, cols, vals = [], [], []
    rhs = np.zeros(n)
    diag = np.zeros(n)
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        ey = eps[ys, xs]
        en = eps[ys + dy, xs + dx]
        w = 0.5 * (ey + en)
        diag += w
        nb = idx[ys + dy, xs + dx]
        free = nb >= 0
        rows.append(me[free]); cols.append(nb[free]); vals.append(-w[free])
        fx = ~free
        rhs[me[fx]] += w[fx] * value[ys[fx] + dy, xs[fx] + dx]
    rows.append(me); cols.append(me); vals.append(diag)
    A = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                   shape=(n, n)).tocsc()
    sol = spsolve(A, rhs)
    phi = value.copy()
    phi[unk] = sol
    return phi, 1, True


def _solve_phi(eps, fixed, value, tol, max_iter):
    """div(eps grad phi) = 0 mit Rot-Schwarz-SOR loesen. Rueckgabe (phi, Iterationen, konvergiert).

    Alle Felder in voller Groesse; wrap-around an den Raendern ist egal,
    weil der Rand fixiert ist und nur Innenzellen aktualisiert werden.
    """
    try:
        from scipy.sparse import coo_matrix  # optional, viel schneller
        from scipy.sparse.linalg import spsolve
    except Exception:
        coo_matrix = None
    if coo_matrix is not None:
        return _solve_phi_sparse(eps, fixed, value, coo_matrix, spsolve)
    phi = value.copy()
    # Gesichts-Permittivitaeten (arithmetisches Mittel am Materialuebergang),
    # als Innenbereich-Slices gehalten: Index (i, j) gehoert zu Zelle (i+1, j+1).
    eps_e = 0.5 * (eps[1:-1, 1:-1] + eps[1:-1, 2:])
    eps_w = 0.5 * (eps[1:-1, 1:-1] + eps[1:-1, :-2])
    eps_n = 0.5 * (eps[1:-1, 1:-1] + eps[2:, 1:-1])
    eps_s = 0.5 * (eps[1:-1, 1:-1] + eps[:-2, 1:-1])
    den = eps_e + eps_w + eps_n + eps_s

    omega = 1.85
    interior = ~fixed
    converged = False
    it = 0
    check_every = 25
    yy, xx = np.indices(phi.shape)
    red = ((yy + xx) % 2 == 0) & interior
    black = ((yy + xx) % 2 == 1) & interior
    inner = np.zeros((phi.shape[0] - 2, phi.shape[1] - 2), dtype=np.float64)
    tmp = np.zeros_like(inner)
    upd_full = np.zeros_like(phi)

    def sweep(mask):
        # num = e_E*phi_E + e_W*phi_W + e_N*phi_N + e_S*phi_S  (Innenbereich)
        np.multiply(eps_e, phi[1:-1, 2:], out=inner)
        np.multiply(eps_w, phi[1:-1, :-2], out=tmp)
        inner[:] += tmp
        np.multiply(eps_n, phi[2:, 1:-1], out=tmp)
        inner[:] += tmp
        np.multiply(eps_s, phi[:-2, 1:-1], out=tmp)
        inner[:] += tmp
        inner[:] /= den
        upd_full[1:-1, 1:-1] = inner
        old = phi[mask]
        upd = old + omega * (upd_full[mask] - old)
        delta = float(np.max(np.abs(upd - old))) if old.size else 0.0
        phi[mask] = upd
        return delta

    for it in range(1, max_iter + 1):
        d1 = sweep(red)
        d2 = sweep(black)
        if it % check_every == 0 and max(d1, d2) < tol:
            converged = True
            break
    return phi, it, converged


def _charge(phi, eps, conductor_mask, value):
    """Gauss-Integral: Ladung pro Laengeneinheit auf dem Leiter (C/m).

    Summe von eps0 * eps_r * (phi_nachbar - V_leiter) ueber alle
    Leiteroberflaechen-Seiten. Nachbarn mit anderem Potential (z. B. die
    Gegenseite eines Diff-Paars) traegen die Koppelkapazitaet bei.
    """
    v_c = float(value[conductor_mask].mean())
    q = 0.0
    for axis in (0, 1):
        for shift in (1, -1):
            # Zelle i ist Leiter, ihr Nachbar in Richtung -shift nicht:
            faces = conductor_mask & ~np.roll(conductor_mask, shift, axis=axis)
            eps_face = 0.5 * (eps + np.roll(eps, shift, axis=axis))
            phi_nb = np.roll(phi, shift, axis=axis)
            q += float(np.sum(eps_face[faces] * (phi_nb[faces] - v_c)))
    return EPS0 * abs(q)


def _solve_once(s: Structure, air_only: bool):
    eps, fixed, value, ny, nx, cell, masks = _build_grid(s, air_only)
    phi, it, conv = _solve_phi(eps, fixed, value, s.tol, s.max_iter)
    charges = [abs(_charge(phi, eps, m, value)) for m in masks]
    return charges, it, conv, (ny, nx), cell


def solve(s: Structure) -> SolveResult:
    """Impedanz(en) eines Querschnitts berechnen."""
    charges_d, it1, conv1, grid, cell = _solve_once(s, air_only=False)
    charges_a, it2, conv2, _, _ = _solve_once(s, air_only=True)

    if s.gap:
        # Odd-Mode: Ladung auf P bei V_P = +1 V
        c_odd = charges_d[0] / 1.0
        c_odd0 = charges_a[0] / 1.0
        z_odd = 1.0 / (C0 * math.sqrt(c_odd * c_odd0))
        zdiff = 2.0 * z_odd
        return SolveResult(
            z0=zdiff / 2.0,
            eps_eff=c_odd / c_odd0,
            capacitance=c_odd,
            capacitance_air=c_odd0,
            zdiff=zdiff,
            eps_eff_odd=c_odd / c_odd0,
            c_odd=c_odd,
            c_odd_air=c_odd0,
            iterations=it1 + it2,
            converged=conv1 and conv2,
            grid=grid,
            cell=cell,
        )
    cap = charges_d[0] / 1.0
    cap0 = charges_a[0] / 1.0
    z0 = 1.0 / (C0 * math.sqrt(cap * cap0))
    return SolveResult(
        z0=z0,
        eps_eff=cap / cap0,
        capacitance=cap,
        capacitance_air=cap0,
        iterations=it1 + it2,
        converged=conv1 and conv2,
        grid=grid,
        cell=cell,
    )
