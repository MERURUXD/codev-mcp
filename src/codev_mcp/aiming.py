"""Pupil aiming map for the plotted spot diagram.

A ray is launched from the first tangent plane, so a pupil grid needs the
launch point that a real, aimed ray takes for each relative pupil coordinate.
For large fields the paraxial entrance pupil is a poor guess: the real pupil
is shifted and distorted, and grid rays launched from the paraxial pupil are
stopped by the aperture. A few rays aimed by CODE V (RAYRSI) fix the map from
relative pupil coordinates to launch points; a cubic polynomial in the two
coordinates then predicts the launch points of the whole grid.

The module only does arithmetic, so it can be tested without CODE V.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def vignetted_pupil(
    u: float, v: float, vux: float, vlx: float, vuy: float, vly: float
) -> tuple[float, float]:
    """Scale a unit pupil coordinate by the vignetting factors.

    A factor is the fraction of the pupil radius that is cut off at that edge:
    the pupil spans -(1 - VLX)..(1 - VUX) in X and -(1 - VLY)..(1 - VUY) in Y,
    which is the pupil CODE V's own spot option traces.
    """
    scaled_u = u * (1.0 - (vux if u >= 0.0 else vlx))
    scaled_v = v * (1.0 - (vuy if v >= 0.0 else vly))
    return scaled_u, scaled_v


def unit_pupil_grid(count: int) -> list[tuple[float, float]]:
    """Cell centred samples of a count x count grid inside the unit circle."""
    samples: list[tuple[float, float]] = []
    for row in range(count):
        for column in range(count):
            u = (column + 0.5) / count * 2.0 - 1.0
            v = (row + 0.5) / count * 2.0 - 1.0
            if u * u + v * v <= 1.0:
                samples.append((u, v))
    return samples


def calibration_nodes() -> list[tuple[float, float]]:
    """The centre plus two rings of eight points on the unit pupil.

    Seventeen nodes give the ten cubic coefficients seven degrees of freedom
    of redundancy, which is what the residual check relies on.
    """
    nodes = [(0.0, 0.0)]
    for radius in (0.5, 1.0):
        for step in range(8):
            angle = step * math.pi / 4.0
            nodes.append((radius * math.cos(angle), radius * math.sin(angle)))
    return nodes


def _terms(u: float, v: float) -> list[float]:
    return [1.0, u, v, u * u, u * v, v * v, u * u * u, u * u * v, u * v * v, v * v * v]


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a small square system."""
    size = len(rhs)
    rows = [matrix[index][:] + [rhs[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-14:
            raise ValueError("The calibration rays do not determine the pupil map.")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(column + 1, size):
            factor = rows[row][column] / rows[column][column]
            for index in range(column, size + 1):
                rows[row][index] -= factor * rows[column][index]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        tail = sum(rows[row][index] * solution[index] for index in range(row + 1, size))
        solution[row] = (rows[row][size] - tail) / rows[row][row]
    return solution


def _fit(design: list[list[float]], target: list[float]) -> list[float]:
    """Least squares through the normal equations."""
    size = len(design[0])
    normal = [
        [sum(row[i] * row[j] for row in design) for j in range(size)] for i in range(size)
    ]
    rhs = [sum(row[i] * value for row, value in zip(design, target)) for i in range(size)]
    return _solve(normal, rhs)


@dataclass(frozen=True)
class PupilMap:
    """Launch point on the first tangent plane for a relative pupil coordinate."""

    x_coefficients: tuple[float, ...]
    y_coefficients: tuple[float, ...]
    #: Largest distance between a calibration ray's launch point and the fit.
    max_residual: float
    nodes: int

    def launch(self, u: float, v: float) -> tuple[float, float]:
        terms = _terms(u, v)
        return (
            sum(c * t for c, t in zip(self.x_coefficients, terms)),
            sum(c * t for c, t in zip(self.y_coefficients, terms)),
        )


def fit_pupil_map(
    samples: list[tuple[float, float, float, float]],
) -> PupilMap:
    """Fit the map from (u, v, launch_x, launch_y) samples.

    At least ten samples are needed for the ten coefficients; the caller
    supplies more so that the residual says something about the fit.
    """
    if len(samples) < 10:
        raise ValueError(
            f"{len(samples)} aimed rays are not enough to fit the pupil map (10 needed)."
        )
    design = [_terms(u, v) for u, v, _, _ in samples]
    x_coefficients = _fit(design, [sample[2] for sample in samples])
    y_coefficients = _fit(design, [sample[3] for sample in samples])
    fitted = PupilMap(tuple(x_coefficients), tuple(y_coefficients), 0.0, len(samples))
    residual = max(
        math.hypot(fitted.launch(u, v)[0] - x, fitted.launch(u, v)[1] - y)
        for u, v, x, y in samples
    )
    return PupilMap(fitted.x_coefficients, fitted.y_coefficients, residual, len(samples))
