"""A small stand-in for a CODE V session, used to test the COM backend logic.

It deliberately reproduces the two behaviours that make the real backend
tricky:

* an unknown database item echoes the previous result instead of failing, so a
  misspelled item silently returns a stale number;
* an edit to a parameter that is controlled by a solve, or a zoom qualified edit
  for a parameter that was never zoomed, is accepted without any error and
  leaves the value unchanged.

Tests assert that the backend detects both situations.
"""

from __future__ import annotations

import json
import math
import re
import struct
import zlib
from pathlib import Path

from codev_mcp import plotting
from codev_mcp.com_session import ERROR_LINE, ComSession
from codev_mcp.errors import ComputationError, ParameterError

INFINITE_RADIUS = "0.1000000000000000E+19"
INFINITE_THICKNESS = 993938392063.0

#: Measured on the real machine: CODE V cuts a plot file filespec at this many
#: characters and only appends the .PLT extender while it still fits.
CODEV_PLOT_FILESPEC_LIMIT = 80


def corrupt_png_idat(data: bytes) -> bytes:
    """Break a PNG's compressed picture data while keeping its chunks valid.

    The chunk layout, the IHDR and every checksum stay correct, so only a
    decompression attempt can tell that the picture is unusable. This is the
    spelling of a broken file that header checks cannot catch.
    """
    offset = len(b"\x89PNG\r\n\x1a\n")
    target: int | None = None
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        if data[offset + 4 : offset + 8] == b"IDAT":
            target = offset
            break
        offset += 12 + length
    if target is None:
        return data
    length = struct.unpack(">I", data[target : target + 4])[0]
    payload = bytearray(data[target + 8 : target + 8 + length])
    # The two byte zlib header stays intact, so the stream starts and then
    # fails, which is what a corrupt picture looks like.
    for index in range(2, min(len(payload), 8)):
        payload[index] ^= 0xFF
    body = b"IDAT" + bytes(payload)
    return (
        data[:target]
        + struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        + data[target + 12 + length :]
    )


def _catalog_parts(value: str | None) -> tuple[str, str]:
    """Split a stored glass into its name and catalog, when it has one."""
    text = (value or "").strip()
    if "_" not in text:
        return text, ""
    name, _, catalog = text.partition("_")
    return name, catalog


def default_surfaces() -> dict[int, dict[str, object]]:
    """A four surface lens: object, two glass surfaces, image."""
    return {
        0: {"radius": None, "thickness": None, "glass": ""},
        1: {"radius": 64.12345678901234, "thickness": 9.12345599999, "glass": "BSM24"},
        2: {"radius": -42.5, "thickness": 4.25, "glass": "SK16"},
        3: {"radius": None, "thickness": 61.23456789123456, "glass": ""},
    }


class FakeCodeVSession:
    """Implements the subset of the session interface that ComBackend uses."""

    def __init__(
        self,
        *,
        surfaces: dict[int, dict[str, object]] | None = None,
        catalog: dict[int, str] | None = None,
        solves: dict[int, str] | None = None,
        fields: list[dict[str, object]] | None = None,
        wavelengths: list[dict[str, object]] | None = None,
        zoom_positions: int = 1,
        reference: int = 2,
        dimension: int = 2,
        title: str = "Fake three surface lens",
        listing: str | None = None,
        max_aperture: float = 28.0,
        aperture_kind: str = "epd",
        aperture_value: float = 50.0,
        surface_apertures: dict[int, dict[str, object]] | None = None,
        pickups: dict[int, str] | None = None,
        spot_blur: float = 0.004,
        spot_statistics_scale: float = 1.0,
        mtf_failure: bool = False,
        async_ticks: int = 0,
        stop_takes_effect: bool = True,
        text_buffer_size: int = 2000000,
        async_output_override: str | None = None,
        plot_file_missing: bool = False,
        plot_file_empty: bool = False,
        plot_file_drops_extender: bool = False,
        plot_file_numbered: bool = False,
        plot_filespec_limit: int = CODEV_PLOT_FILESPEC_LIMIT,
        plot_command_error: bool = False,
        plot_empty_output: bool = False,
        async_command_fails: bool = False,
        gcv_fails: bool = False,
        gcv_corrupt_png: bool = False,
        gcv_corrupts_idat: bool = False,
        gcv_truncates_png_to: int = 0,
        gra_reset_fails: bool = False,
        wait_completes_while_executing: bool = False,
    ) -> None:
        self.surfaces = surfaces or default_surfaces()
        self.stop_surface = 1
        self._modelled = False
        self.catalog = catalog or {1: "OHARA"}
        #: Per zoom position values, kept apart from the plain surface data so a
        #: saved and restored session hands back the same zoomed lens.
        self.zoom_values: dict[int, dict[str, dict[int, float]]] = {}
        self.solves = solves or {}
        self.fields = fields or [
            {"x": 0.0, "y": 0.0, "weight": 1.0},
            {"x": 0.0, "y": 10.0, "weight": 1.0},
        ]
        self.wavelengths = wavelengths or [
            {"nm": 656.3, "weight": 1.0},
            {"nm": 587.6, "weight": 1.0},
        ]
        self.zoom_positions = zoom_positions
        self.reference = reference
        self.dimension = dimension
        self.title = title
        self.max_aperture = max_aperture
        self.aperture_kind = aperture_kind.lower()
        self.aperture_value = float(aperture_value)
        self.surface_apertures = {
            int(number): dict(value) for number, value in (surface_apertures or {}).items()
        }
        self._custom_listing = listing is not None
        self.listing = listing if listing is not None else self._build_listing()
        self.pickups = pickups or {}
        self.spot_blur = spot_blur
        self.spot_statistics_scale = spot_statistics_scale
        self.mtf_failure = mtf_failure
        self.async_ticks = async_ticks
        self.stop_takes_effect = stop_takes_effect
        self.text_buffer_size = text_buffer_size
        self.async_output_override = async_output_override
        self.plot_file_missing = plot_file_missing
        self.plot_file_empty = plot_file_empty
        self.plot_file_drops_extender = plot_file_drops_extender
        self.plot_file_numbered = plot_file_numbered
        self.plot_filespec_limit = plot_filespec_limit
        self.plot_command_error = plot_command_error
        self.plot_empty_output = plot_empty_output
        self.async_command_fails = async_command_fails
        self.gcv_fails = gcv_fails
        self.gcv_corrupt_png = gcv_corrupt_png
        self.gcv_corrupts_idat = gcv_corrupts_idat
        self.gcv_truncates_png_to = gcv_truncates_png_to
        self.gra_reset_fails = gra_reset_fails
        self.wait_completes_while_executing = wait_completes_while_executing
        self.async_output: str = ""
        self.stop_requested = False
        self._async_ticks_left = 0
        self._async_running = False
        #: Whether the running option was started while a plot file was open.
        self._async_plot = False
        #: The option's completion is reported as complete once, before it really
        #: is, which is the behaviour the real engine showed.
        self._wait_lie_used = False
        #: The plot file the GRA command opened, and whether it is still open.
        self._graphics_file: Path | None = None
        self._graphics_open = False
        #: Every call the backend makes, in order, so a test can assert that the
        #: option output was read before the graphics state was touched.
        self.events: list[str] = []
        self.gcv_calls: list[str] = []
        self.first_order = {
            "EFY": 100.0,
            "FNO": 2.0,
            "EPD": 50.0,
            "ENP": 55.87,
            "EXP": -51.61,
            "EXD": 57.38,
            "IMD": 63.14,
            "OAL": 75.04,
            "AFC": 0.0,
        }
        self.traced_points: list[tuple[float, float]] = []
        self.mtf_calls: list[tuple[int, int, float, float]] = []
        self.raytra_calls: list[list[float]] = []
        self.rayrsi_calls: list[tuple[int, int, int, list[float]]] = []
        #: The real pupil differs from the paraxial one: the launch point of a
        #: ray at relative pupil (u, v) is shifted along the field direction by
        #: pupil_shift and bent by cubic/quintic terms (fractions of the pupil
        #: radius). All zero reproduces the paraxial entrance pupil.
        self.pupil_shift = 0.0
        self.pupil_cubic = 0.0
        self.pupil_quintic = 0.0
        #: RAYTRA blocks a ray whose relative pupil radius exceeds this.
        self.aperture_limit = 1.0 + 1e-9
        #: Where the chief ray lands on the image surface (an off axis field).
        self.image_offset = (0.0, 0.0)
        #: Returns the RAYRSI status (0 = success) for a relative pupil point.
        self.rayrsi_status = lambda u, v: 0.0
        self._ray_s1: tuple[float, float, float] | None = None
        #: Faults the phase G tests inject. Kept in the fake because the real
        #: session has no such knobs, and every fault mode is opt in.
        self.faults: dict[str, int] = {}
        self.fault_log: list[str] = []
        # An injected session is already running; start() is only used when the
        # backend owns the session.
        self.started = True
        #: The watchdog flag and process set of the real session: a test sets
        #: engine_dead to model the engine having exited.
        self.engine_dead = False
        self.engine_pids: set[int] = set()
        self.commands: list[str] = []
        self.evaluations: list[str] = []
        #: COM call counter, reported in the status details like the real one.
        self.call_count = 0
        self._last_value = "0"
        self._saved: dict[str, dict] = {}

    # ----------------------------------------------------------- session api

    def start(self) -> str:
        self.started = True
        return "10.2;Build (207)"

    def stop(self) -> None:
        self.started = False

    def get_version(self) -> str:
        return "10.2;Build (207)"

    def get_surface_count(self) -> int:
        return len(self.surfaces)

    def get_dimension(self) -> int:
        return self.dimension

    def get_stop_surface(self) -> int:
        return self.stop_surface

    def get_zoom_count(self) -> int:
        return self.zoom_positions

    def get_field_count(self) -> int:
        return len(self.fields)

    def get_wavelength_count(self) -> int:
        return len(self.wavelengths)

    def get_max_aperture(self, surface: int, zoom: int = 1) -> float:
        entry = self.surface_apertures.get(surface)
        if entry and entry.get("kind", "clear") == "clear" and entry.get("radius") is not None:
            return float(entry["radius"])
        return self.max_aperture

    # ------------------------------------------------------------- evaluating

    def evaluate(self, item: str) -> str:
        self.call_count += 1
        self.evaluations.append(item)
        text = item.strip()
        if not (text.startswith("(") and text.endswith(")")):
            return self._last_value
        body = text[1:-1].strip()
        value = self._evaluate_body(body)
        if value is None:
            # CODE V echoes the previous result for an item it does not know.
            return self._last_value
        self._last_value = value
        return value

    def evaluate_number(self, item: str) -> float:
        return float(self.evaluate(item))

    def evaluate_optional(self, item: str) -> str:
        return self.evaluate(item)

    def _evaluate_body(self, body: str) -> str | None:
        match = re.match(r"^NUM S$", body)
        if match:
            return str(max(self.surfaces))
        if body == "TIT":
            return self.title
        if body == "REF":
            return str(self.reference)
        match = re.match(r"^(EPD|FNO|NA|NAO) Z(\d+)$", body)
        if match:
            kind = match.group(1).lower()
            if kind == self.aperture_kind:
                return f"{self.aperture_value:.16g}"
            if kind == "epd":
                return f"{float(self.first_order['EPD']):.16g}"
            if kind == "fno":
                return f"{float(self.first_order['FNO']):.16g}"
            if kind == "na":
                return f"{1.0 / (2.0 * float(self.first_order['FNO'])):.16g}"
            return "1e-10"

        match = re.match(r"^(RDY|THI) S(\d+)(?: Z(\d+))?$", body)
        if match:
            item, number, zoom = match.group(1), int(match.group(2)), match.group(3)
            surface = self.surfaces.get(number)
            if surface is None:
                return None
            key = "radius" if item == "RDY" else "thickness"
            zoom_map = (surface.get("zoom") or {}).get(key)
            if zoom_map:
                zoom_key = int(zoom or 1)
                if zoom_key not in zoom_map:
                    return None
                return f"{float(zoom_map[zoom_key]):.16g}"
            value = surface[key]
            if value is None:
                return INFINITE_RADIUS if key == "radius" else f"{INFINITE_THICKNESS:.1f}"
            return f"{float(value):.16g}"

        match = re.match(r"^GLA S(\d+)( CAT)?$", body)
        if match:
            number = int(match.group(1))
            surface = self.surfaces.get(number)
            if surface is None:
                return None
            name, attached = _catalog_parts(str(surface["glass"]))
            if match.group(2):
                # The catalog of a glass saved by the session comes back with
                # the file, exactly as CODE V keeps it in the lens.
                return attached or self.catalog.get(number, "")
            return name

        match = re.match(r"^TYP SOL S(\d+) (THI|CUY|CUX)$", body)
        if match:
            return self.solves.get(int(match.group(1)), " ")

        match = re.match(r"^(XAN|YAN|WTF|VUX|VLX|VUY|VLY) F(\d+)(?: Z(\d+))?$", body)
        if match:
            item, number = match.group(1), int(match.group(2))
            if not 1 <= number <= len(self.fields):
                return None
            key = {"XAN": "x", "YAN": "y", "WTF": "weight"}.get(item, item.lower())
            return f"{float(self.fields[number - 1].get(key, 0.0)):.16g}"

        match = re.match(r"^(WL|WTW) W(\d+)$", body)
        if match:
            item, number = match.group(1), int(match.group(2))
            if not 1 <= number <= len(self.wavelengths):
                return None
            key = "nm" if item == "WL" else "weight"
            return f"{float(self.wavelengths[number - 1][key]):.16g}"

        match = re.match(r"^(EFY|FNO|EPD|ENP|EXP|EXD|IMD|OAL|AFC) Z(\d+)$", body)
        if match:
            return f"{float(self.first_order[match.group(1)]):.16g}"
        match = re.match(r"^([XYZ]) S1$", body)
        if match and self._ray_s1 is not None:
            return f"{self._ray_s1['XYZ'.index(match.group(1))]:.16g}"
        return None

    # --------------------------------------------------------- analysis calls

    def MTF_1FLD(
        self,
        zoom: int,
        field: int,
        frequency: float,
        azimuth: float,
        nrd: int,
        values: list[float],
        mtf_type: int = 0,
        mtf_wave: int = 0,
    ):
        self.mtf_calls.append((int(zoom), int(field), float(frequency), float(azimuth)))
        if self.mtf_failure:
            return (-1.0, list(values))
        f_number = self.first_order["FNO"]
        wavelength_mm = 0.5876 / 1000.0
        cutoff = 1.0 / (wavelength_mm * f_number)
        ratio = min(frequency / cutoff, 1.0)
        modulation = (2.0 / math.pi) * (
            math.acos(ratio) - ratio * math.sqrt(max(1.0 - ratio * ratio, 0.0))
        )
        modulation *= 1.0 - 0.05 * (field - 1)
        if azimuth >= 45.0:
            modulation = min(1.0, modulation * 1.02)
        filled = [modulation, 0.0, 1.0, 1.0, 1.0, float(nrd or 60)]
        values[:] = filled
        return (modulation, filled)

    def _launch_point(
        self, u: float, v: float, direction_x: float, direction_y: float
    ) -> tuple[float, float]:
        radius = self.first_order["EPD"] / 2.0
        distance = self.first_order["ENP"] + self.pupil_shift
        return (
            radius * (u + self.pupil_cubic * u ** 3 + self.pupil_quintic * u ** 5)
            + direction_x * distance,
            radius * (v + self.pupil_cubic * v ** 3 + self.pupil_quintic * v ** 5)
            + direction_y * distance,
        )

    def _pupil_from_launch(
        self, x: float, y: float, direction_x: float, direction_y: float
    ) -> tuple[float, float]:
        """Invert _launch_point (each axis is monotonic for small terms)."""
        radius = self.first_order["EPD"] / 2.0
        distance = self.first_order["ENP"] + self.pupil_shift

        def solve(target: float) -> float:
            value = target
            for _ in range(60):
                error = value + self.pupil_cubic * value ** 3 + self.pupil_quintic * value ** 5 - target
                slope = 1.0 + 3.0 * self.pupil_cubic * value ** 2 + 5.0 * self.pupil_quintic * value ** 4
                value -= error / slope
            return value

        return (
            solve((x - direction_x * distance) / radius),
            solve((y - direction_y * distance) / radius),
        )

    def RAYRSI(self, zoom: int, wavelength: int, field: int, reference: int, inputs: list):
        u, v = float(inputs[0]), float(inputs[1])
        self.rayrsi_calls.append((int(zoom), int(wavelength), int(field), [float(i) for i in inputs]))
        status = float(self.rayrsi_status(u, v))
        entry = self.fields[field - 1]
        direction_x = math.tan(math.radians(entry.get("x", 0.0)))
        direction_y = math.tan(math.radians(entry.get("y", 0.0)))
        x, y = self._launch_point(u, v, direction_x, direction_y)
        # The ray meets the first surface a sag further along its direction.
        sag = 0.002 * (x * x + y * y)
        self._ray_s1 = (x + sag * direction_x, y + sag * direction_y, sag)
        return status

    def RAYTRA(self, zoom: int, wavelength: int, aperture_check: int, inputs: list, outputs: list):
        self.raytra_calls.append(list(inputs))
        x, y, direction_x, direction_y = (float(value) for value in inputs)
        u, v = self._pupil_from_launch(x, y, direction_x, direction_y)
        image_x = self.spot_blur * u + self.image_offset[0]
        image_y = self.spot_blur * v + self.image_offset[1]
        filled = [image_x, image_y, 0.0, 0.0, 0.0, 1.0, 10.0, 1.0]
        if aperture_check and math.hypot(u, v) > self.aperture_limit:
            outputs[:] = filled
            return (-6.0, filled)
        self.traced_points.append((image_x, image_y))
        outputs[:] = filled
        return (0.0, filled)

    def stop_command(self) -> None:
        self.events.append("stop_command")
        self.commands.append("StopCommand")
        self.stop_requested = True
        if self.stop_takes_effect:
            self._async_ticks_left = 0
            self._async_running = False

    # ------------------------------------------------------- asynchronous mode

    def async_command(self, text: str) -> None:
        self.events.append(f"async_command {text}")
        if self.async_command_fails:
            raise ComputationError(f"The asynchronous command {text!r} was refused.")
        self.commands.append(f"AsyncCommand {text}")
        self._async_running = True
        self._async_ticks_left = self.async_ticks
        self._async_plot = self._graphics_open
        self.stop_requested = False
        if self.plot_empty_output:
            self.async_output = ""
        elif self.plot_command_error:
            self.async_output = "Error:   This option could not be executed\r\n"
        elif self.async_output_override is not None:
            self.async_output = self.async_output_override
        elif text.lower().startswith("spo"):
            self.async_output = self._spot_statistics_listing()
        else:
            self.async_output = "Command End:\r\n"

    def is_executing_command(self) -> bool:
        self.events.append("is_executing_command")
        return self._async_running and self._async_ticks_left > 0

    def wait(self, seconds: int) -> int:
        self.events.append(f"wait {seconds}")
        if not self._async_running:
            return 0
        if self.stop_requested and not self.stop_takes_effect:
            # A stop that is ignored leaves the calculation running forever.
            return 1
        if self.wait_completes_while_executing and not self._wait_lie_used:
            # The real engine reported "completed" while the option was in fact
            # still executing. The lie is told once, so the poll loop that
            # ignores it can then see the real completion.
            self._wait_lie_used = True
            return 0
        if self._async_ticks_left > 0:
            self._async_ticks_left -= 1
            if self._async_ticks_left > 0:
                return 1
        self._finish_async()
        return 0

    def _finish_async(self) -> None:
        """Finish the option and append its plot to the open plot file."""
        self._async_running = False
        if not self._async_plot or self._graphics_file is None:
            return
        if self.plot_file_missing or self.plot_file_empty:
            # The option produced no plot data at all: either the file was never
            # written, or it exists but stays empty.
            return
        with self._graphics_file.open("ab") as handle:
            handle.write(b"CODE V neutral plot data\r\n")

    def get_command_output(self) -> str:
        self.events.append("get_command_output")
        return self.async_output

    def output_is_truncated(self, text: str) -> bool:
        return len(text) >= max(self.text_buffer_size - 1, 1)

    # The backend talks to the session wrapper, so the fake exposes the same
    # wrapper methods and reuses the real result normalisation.

    def mtf_1fld(
        self, zoom: int, field: int, frequency: float, azimuth: float, nrd: int,
        mtf_type: int = 0, mtf_wave: int = 0,
    ):
        values = [0.0] * 6
        return ComSession._normalise_array_result(
            self.MTF_1FLD(zoom, field, frequency, azimuth, nrd, values, mtf_type, mtf_wave)
        )

    def rayrsi(self, zoom: int, wavelength: int, field: int, inputs: list) -> float:
        return self.RAYRSI(zoom, wavelength, field, 0, inputs)

    def raytra(self, zoom: int, wavelength: int, aperture_check: int, inputs: list):
        outputs = [0.0] * 8
        return ComSession._normalise_array_result(
            self.RAYTRA(zoom, wavelength, aperture_check, inputs, outputs)
        )

    # -------------------------------------------------------------- commands

    def command(
        self, text: str, error_kind: type[Exception] | None = None, **_ignored
    ) -> str:
        """Run a command, optionally raising on an Error line like the real session.

        ComSession turns any output line that starts with "Error:" into an
        exception; the fake keeps that opt in, because most tests read the raw
        text and only the graphics commands depend on the failure being raised.
        """
        output = self._run_command(text)
        if error_kind is not None:
            errors = [
                line.strip() for line in output.splitlines() if ERROR_LINE.match(line)
            ]
            if errors:
                raise error_kind(errors[0], details={"command": text}, raw_output=output)
        return output

    def _run_command(self, text: str) -> str:
        self.call_count += 1
        self.commands.append(text)
        self.events.append(f"command {text}")
        if "\n" in text:
            raise ParameterError("newline in command")
        head = text.strip()

        if head.lower() == "len":
            self._modelled = True
            self.surfaces = {0: {"radius": None, "thickness": None, "glass": ""},
                             1: {"radius": None, "thickness": None, "glass": ""}}
            self.stop_surface = 1
            self.catalog = {}
            self.solves = {}
            self.pickups = {}
            self.surface_apertures = {}
            self.fields = []
            self.wavelengths = []
            self.reference = 1
            self.listing = self._build_listing()
            return "Command End:\r\n"
        if self._modelled:
            match = re.fullmatch(r"dim ([MCI])", head, re.IGNORECASE)
            if match:
                self.dimension = {"M": 2, "C": 1, "I": 0}[match.group(1).upper()]
                self.listing = self._build_listing()
                return "Command End:\r\n"
            if head.lower() == "rdm":
                return "Command End:\r\n"
            match = re.fullmatch(r"(wl|xan|yan) (.+)", head, re.IGNORECASE)
            if match and not re.search(r"\b[WF]\d+\b", head, re.IGNORECASE):
                values = [float(item) for item in match.group(2).split()]
                kind = match.group(1).lower()
                if kind == "wl":
                    self.wavelengths = [{"nm": item, "weight": 1.0} for item in values]
                else:
                    self._resize_fields(len(values))
                    for field, value in zip(self.fields, values):
                        field["x" if kind == "xan" else "y"] = value
                self.listing = self._build_listing()
                return "Command End:\r\n"
            if head.lower() == "pim yes":
                last = max(self.surfaces) - 1
                if last < 1:
                    return "Error:   Invalid lens - less than 3 surfaces\r\nCommand End:\r\n"
                self.solves = {last: "PIM"}
                self.listing = self._build_listing()
                return "Command End:\r\n"
            match = re.fullmatch(r"ins (si|s\d+) (\S+) (\S+)(?: (\S+))?", head, re.IGNORECASE)
            if match:
                before = max(self.surfaces) if match.group(1).lower() == "si" else int(match.group(1)[1:])
                if not 1 <= before <= max(self.surfaces):
                    return "Error:   Surface qualifier out of range\r\n"
                self.surfaces = {number if number < before else number + 1: item
                                 for number, item in self.surfaces.items()}
                radius = float(match.group(2))
                self.surfaces[before] = {"radius": None if radius == 0 else radius,
                                         "thickness": float(match.group(3)),
                                         "glass": match.group(4) or ""}
                if self.stop_surface >= before:
                    self.stop_surface += 1
                self.listing = self._build_listing()
                return "Command End:\r\n"
            match = re.fullmatch(r"del s(\d+)", head, re.IGNORECASE)
            if match:
                number = int(match.group(1))
                if not 1 <= number < max(self.surfaces):
                    return "Error:   Surface qualifier out of range\r\n"
                self.surfaces.pop(number)
                self.surfaces = {old if old < number else old - 1: item
                                 for old, item in self.surfaces.items()}
                if self.stop_surface > number:
                    self.stop_surface -= 1
                elif self.stop_surface == number:
                    self.stop_surface = 1
                self.listing = self._build_listing()
                return "Command End:\r\n"
            match = re.fullmatch(r"sto s(\d+)", head, re.IGNORECASE)
            if match:
                self.stop_surface = int(match.group(1))
                self.listing = self._build_listing()
                return "Command End:\r\n"

        if head.lower().startswith("lis"):
            return self.listing
        if head.lower().startswith("spo"):
            return self._spot_statistics_listing()
        if head.lower().startswith("wav"):
            return self._wavefront_listing()
        if head.lower().startswith("sav "):
            target = self._filespec(head[4:])
            self._saved[str(target).lower()] = self._snapshot()
            Path(target).write_text(json.dumps(self._snapshot()), encoding="utf-8")
            return f"     System saved in file {target}\r\nCommand End:\r\n"
        if head.lower().startswith("res "):
            target = self._filespec(head[4:])
            snapshot = self._saved.get(str(target).lower())
            path = Path(target)
            if snapshot is None:
                if not path.exists():
                    return "Error:   File not found\r\nCommand End:\r\n"
                try:
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    # A lens file this fake does not model: report a successful
                    # restore and leave the current state alone.
                    return f"    File {target} has been restored\r\nCommand End:\r\n"
            self._load(snapshot)
            return f"    File {target} has been restored\r\nCommand End:\r\n"

        if head.lower().startswith("gcv "):
            return self._convert_plot(head)

        if re.match(r"^gra(\s|$)", head, re.IGNORECASE):
            return self._direct_graphics(head)

        match = re.match(r"^(EPD|FNO|NA|NAO) (-?[\d.Ee+]+)$", head, re.IGNORECASE)
        if match:
            kind = match.group(1).lower()
            value = float(match.group(2))
            if kind != self.aperture_kind:
                self.aperture_kind = kind
            self.aperture_value = value
            if kind == "epd":
                self.first_order["EPD"] = value
            elif kind == "fno":
                self.first_order["FNO"] = value
            if not self._custom_listing:
                self.listing = self._build_listing()
            return "Command End:\r\n"

        match = re.match(
            r"^CIR S(\d+) CLR(?: L'([^']{1,3})')? (-?[\d.Ee+]+)$",
            head,
            re.IGNORECASE,
        )
        if match:
            surface = int(match.group(1))
            current = self.surface_apertures.get(surface)
            label = match.group(2)
            if current is None or current.get("kind", "clear") != "clear":
                self.surface_apertures[surface] = {
                    "kind": "clear",
                    "shape": "circular",
                    "label": label,
                    "radius": float(match.group(3)),
                }
            elif current.get("label") == label:
                current["radius"] = float(match.group(3))
            if not self._custom_listing:
                self.listing = self._build_listing()
            return "Command End:\r\n"

        match = re.match(r"^(RDY|THI) S(\d+)(?: Z(\d+))? (-?[\d.]+)$", head)
        if match:
            return self._set_surface(match.group(1), int(match.group(2)), match.group(3), float(match.group(4)))

        match = re.match(r"^GLA S(\d+)(?: Z(\d+))? (\S+)$", head)
        if match:
            number = int(match.group(1))
            if number not in self.surfaces:
                return "Error:   Surface qualifier out of range\r\nCommand End:\r\n"
            name, catalog = _catalog_parts(match.group(3))
            if not catalog:
                catalog = self.catalog.get(number, "")
                if catalog and not name.endswith("_" + catalog):
                    name = f"{name}_{catalog}"
            self.surfaces[number]["glass"] = name
            if catalog:
                self.catalog[number] = catalog
            return "Command End:\r\n"

        match = re.match(r"^(XAN|YAN) (-?[\d.]+(?: -?[\d.]+)*)$", head)
        if match:
            values = [float(item) for item in match.group(2).split()]
            self._resize_fields(len(values))
            for field, value in zip(self.fields, values):
                field["x" if match.group(1) == "XAN" else "y"] = value
            if not self._custom_listing:
                self.listing = self._build_listing()
            return "Command End:\r\n"

        match = re.match(r"^(XAN|YAN|WTF|VUX|VLX|VUY|VLY) F(\d+)(?: Z(\d+))? (-?[\d.]+)$", head)
        if match:
            item, number = match.group(1), int(match.group(2))
            key = {"XAN": "x", "YAN": "y", "WTF": "weight"}.get(item, item.lower())
            self.fields[number - 1][key] = float(match.group(4))
            if key.startswith("v") and not self._custom_listing:
                self.listing = self._build_listing()  # LIS prints the vignetting rows
            return "Command End:\r\n"

        match = re.match(r"^WL W(\d+) (-?[\d.]+)$", head)
        if match:
            number = int(match.group(1))
            nanometers = float(match.group(2))
            if not 10 <= nanometers <= 1_000_000:
                return "Error:   Invalid data - wavelength out of range\r\nCommand End:\r\n"
            self.wavelengths[number - 1]["nm"] = nanometers
            return "Command End:\r\n"

        match = re.match(r"^WTW W(\d+) (\d+)$", head)
        if match:
            self.wavelengths[int(match.group(1)) - 1]["weight"] = float(match.group(2))
            return "Command End:\r\n"
        if re.match(r"^WTW W\d+ [\d.]+$", head):
            return "Error:   Invalid data - expecting integer data\r\nCommand End:\r\n"

        match = re.match(r"^REF (\d+)$", head)
        if match:
            self.reference = int(match.group(1))
            return "Command End:\r\n"

        return "Error:   Invalid command\r\nCommand End:\r\n"

    def _wavefront_listing(self) -> str:
        rays = [400 - index * 40 for index in range(len(self.fields))]
        lines = ["W A V E F R O N T   A N A L Y S I S", "POSITION 1",
                 "NUMBER OF RAYS " + " ".join(map(str, rays)),
                 "WAVELENGTHS " + " ".join(str(w["nm"]) for w in self.wavelengths),
                 "FIELD                   RMS         SHIFT         STREHL",
                 "      FRACT    DEG      (WAVES)      (MM.)"]
        for index, field in enumerate(self.fields):
            lines += [f"X 0.00 0.00 0.000000",
                      f"Y 0.00 {field['y']:.2f} {0.1 + index * 0.05:.6f} 0.000000 {0.8 - index * 0.1:.6f}"]
        lines += ["WEIGHTED RMS 0.125000 0.700000", "Command End:"]
        return "\r\n".join(lines) + "\r\n"

    def _spot_listing(self) -> str:
        """Spot diagram annotations computed from the rays the fake traced."""
        # The blur is a disk of radius spot_blur, so the largest radius is the
        # disk radius and the RMS radius over a uniform disk is R/sqrt(2).
        max_radius = self.spot_blur
        rms_radius = self.spot_blur / math.sqrt(2.0)
        scale = self.spot_statistics_scale
        lines = [
            "FIELD",
            "POSITION",
            "DEFOCUSING 0.00000",
            self.title,
            ".100E-01 MM",
        ]
        for _ in range(len(self.fields)):
            lines.append(f" 100% =    {2 * max_radius * scale:.6f}")
            lines.append(f" RMS  =    {2 * rms_radius * scale:.6f}")
        lines.append("Command End:")
        return "\r\n".join(lines) + "\r\n"

    def _spot_statistics_listing(self) -> str:
        """The field by field statistics block the SPO option really prints."""
        max_radius = self.spot_blur
        rms_radius = self.spot_blur / math.sqrt(2.0)
        scale = self.spot_statistics_scale
        lines = [
            "    SPO",
            "",
            "    RAY STATISTICS FOR FIELD  1 ZOOM  1 ON PLOT:",
            "",
            "                                       POINTS     POINTS",
            "           WAVELENGTH         WEIGHT   TRACED   ATTEMPTED",
            "",
        ]
        for wavelength in self.wavelengths:
            lines.append(
                f"              {wavelength['nm']:.1f}              1       112       140"
            )
        lines.append("")
        for position, field in enumerate(self.fields):
            rays = 1123 + 111 * position
            lines.append(
                f"       Field  {position + 1}, ( {field['x']:6.2f}, {field['y']:6.2f}) "
                f"degrees.  Focus  0.00000     {rays} Rays"
            )
            lines.append(
                "       Displacement of centroid                    "
                "Minimum RMS spot diameter"
            )
            lines.append(
                f"        X:   0.00000E+00     Y:   {position * 5.3617E-04:.5E}   "
                f"      {2 * rms_radius * scale:.5E} MM"
            )
            lines.append(
                "       Displacement of center of 100% Spot         "
                "Minimum 100% spot diameter"
            )
            lines.append(
                f"        X:   0.00000E+00     Y:   {position * 7.5593E-02:.5E}   "
                f"      {2 * max_radius * scale:.5E} MM"
            )
            lines.append("")
        lines.append("Command End:")
        return "\r\n".join(lines) + "\r\n"

    def command_raw(self, text: str) -> str:
        return self._run_command(text)

    # ------------------------------------------------------- graphics commands

    def _direct_graphics(self, command_text: str) -> str:
        """Model GRA: open a fresh plot file, or close the current one.

        The real command answers "T" with the terminal and a filespec with the
        plot file, closing whatever file was open before. An existing file is
        refused, because that is the case where the real session stops to ask a
        question, which a windowless session must never reach.
        """
        body = command_text[3:].strip()
        parts = body.split(None, 1)
        first = parts[0].lower() if parts else ""
        if not body or (first == "t" and len(parts) == 1):
            if self.gra_reset_fails:
                return "Error:   Graphics output could not be released\r\n"
            self._graphics_open = False
            self._graphics_file = None
            return "Graphics directed to the terminal\r\nCommand End:\r\n"
        filespec = parts[-1].strip().strip('"')
        if not filespec or filespec == "*":
            return "Error:   No graphics file was specified\r\n"
        path = self._plot_filespec(Path(filespec))
        if path.exists():
            return "Error:   File already exists\r\n"
        if not self.plot_file_missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
            self._graphics_file = path
        else:
            # The option "draws" but no plot file is ever written.
            self._graphics_file = path
        self._graphics_open = True
        return f"Graphics directed to file {path.name}\r\nCommand End:\r\n"

    def _plot_filespec(self, requested: Path) -> Path:
        """Apply CODE V's plot file filespec rule to a requested path.

        The real session cut the whole filespec at 80 characters and appended
        the .PLT extender only while it still fitted, so a long result
        directory can leave a plot file without the extender. The extra faults
        model the two spellings that were seen besides the expected one.
        """
        room = max(self.plot_filespec_limit - len(str(requested.parent)) - 1, 1)
        name = requested.name
        if "." not in name and len(name) + len(".PLT") <= room:
            name += ".PLT"
        name = name[:room].rstrip(".")
        if self.plot_file_drops_extender and name.upper().endswith(".PLT"):
            name = name[: -len(".PLT")]
        if self.plot_file_numbered:
            # Seen once on the real machine, when a plot file name was reused
            # inside one session: <name>.1.PLT.
            base = name[: -len(".PLT")] if name.upper().endswith(".PLT") else name
            name = base + ".1.PLT"
        return requested.parent / name if name else requested

    def _convert_plot(self, command_text: str) -> str:
        """Model GCV PNG: convert the neutral plot file beside itself."""
        parts = command_text.split(None, 2)
        target = parts[2].strip().strip('"') if len(parts) > 2 else ""
        self.gcv_calls.append(target)
        if self.gcv_fails:
            return "Error:   The plot file could not be converted\r\n"
        path = Path(target)
        if "." not in path.name:
            # The conversion resolves the logical name, as the real one did
            # when it reported that <name>.plt did not exist.
            path = path.with_name(path.name + ".PLT")
        if not path.exists() or path.stat().st_size == 0:
            return f"Error:   Plot file {path} does not exist.\r\n"
        png = path.with_suffix(".png")
        if self.gcv_corrupt_png:
            png.write_bytes(b"<html>this is not a picture</html>")
        else:
            png.write_bytes(plotting.Canvas(48, 32).to_png())
        if self.gcv_truncates_png_to:
            # A conversion that was cut off: the header can still look valid.
            png.write_bytes(png.read_bytes()[: self.gcv_truncates_png_to])
        if self.gcv_corrupts_idat:
            # A conversion whose picture data is unusable while every chunk
            # checksum still matches.
            png.write_bytes(corrupt_png_idat(png.read_bytes()))
        return f"Converted {path.name} to {png.name}\r\nCommand End:\r\n"

    # --------------------------------------------------------------- helpers

    def _resize_fields(self, count: int) -> None:
        """XAN and YAN set the field count: fields keep their data by number."""
        del self.fields[count:]
        while len(self.fields) < count:
            self.fields.append({"x": 0.0, "y": 0.0, "weight": 1.0})

    def _set_surface(self, item: str, number: int, zoom: str | None, value: float) -> str:
        if number not in self.surfaces:
            return "Error:   Surface qualifier out of range\r\nCommand End:\r\n"
        if number in self.pickups:
            # Pickup controlled: CODE V accepts the command and changes nothing.
            return "Command End:\r\n"
        if number in self.solves and item == "THI":
            # A solve controlled parameter is accepted without any complaint and
            # the value does not change.
            return "Command End:\r\n"
        key = "radius" if item == "RDY" else "thickness"
        zoom_map = (self.surfaces[number].get("zoom") or {}).get(key)
        if zoom:
            if not self._is_zoomed(item, number):
                # A zoom qualified edit for a parameter that was never zoomed is
                # accepted and ignored, exactly as the real session behaved.
                return "Command End:\r\n"
            zoom_map[int(zoom)] = value
            # Zoomed values live in one place, so a snapshot of the session and
            # the values the engine reports can never drift apart.
            self.zoom_values.setdefault(number, {}).setdefault(key, {})[int(zoom)] = value
            return "Command End:\r\n"
        if zoom_map:
            return "Command End:\r\n"
        if value >= 1e17:
            self.surfaces[number][key] = None
        else:
            self.surfaces[number][key] = value
        return "Command End:\r\n"

    def _is_zoomed(self, item: str, number: int) -> bool:
        key = "radius" if item == "RDY" else "thickness"
        return bool((self.surfaces.get(number, {}).get("zoom") or {}).get(key))

    def zoom_surface(self, number: int, key: str, values: dict[int, float]) -> None:
        """Mark a parameter as zoomed, with one value per zoom position."""
        stored = {int(k): float(v) for k, v in values.items()}
        self.zoom_values.setdefault(number, {})[key] = stored
        self.surfaces[number].setdefault("zoom", {})[key] = stored

    @staticmethod
    def _filespec(text: str) -> str:
        return text.strip().strip('"')

    def _snapshot(self) -> dict:
        return {
            "surfaces": {
                str(k): {
                    name: value for name, value in surface.items() if name != "zoom"
                }
                for k, surface in self.surfaces.items()
            },
            "zoom": {
                str(number): {
                    key: {str(position): value for position, value in values.items()}
                    for key, values in items.items()
                }
                for number, items in self.zoom_values.items()
            },
            "catalog": {str(number): name for number, name in self.catalog.items()},
            "fields": [dict(item) for item in self.fields],
            "wavelengths": [dict(item) for item in self.wavelengths],
            "reference": self.reference,
            "aperture_kind": self.aperture_kind,
            "aperture_value": self.aperture_value,
            "surface_apertures": json.loads(json.dumps(self.surface_apertures)),
            "stop_surface": self.stop_surface,
            "dimension": self.dimension,
            "modelled": self._modelled,
            "solves": {str(number): name for number, name in self.solves.items()},
        }

    def _load(self, snapshot: dict) -> None:
        self.surfaces = {int(k): json.loads(json.dumps(v)) for k, v in snapshot["surfaces"].items()}
        if "zoom" in snapshot:
            # A lens saved by this stand in restores its zoomed values and glass
            # catalogs, which is what the real CODE V file does as well.
            self.zoom_values = {
                int(number): {
                    key: {int(position): value for position, value in values.items()}
                    for key, values in items.items()
                }
                for number, items in snapshot["zoom"].items()
            }
        for number, items in self.zoom_values.items():
            for key, values in items.items():
                self.surfaces.setdefault(number, {}).setdefault("zoom", {})[key] = dict(values)
        if "catalog" in snapshot:
            self.catalog = {int(number): name for number, name in snapshot["catalog"].items()}
        self.fields = [dict(item) for item in snapshot["fields"]]
        self.wavelengths = [dict(item) for item in snapshot["wavelengths"]]
        self.reference = snapshot["reference"]
        self.aperture_kind = snapshot.get("aperture_kind", self.aperture_kind)
        self.aperture_value = float(snapshot.get("aperture_value", self.aperture_value))
        self.surface_apertures = {
            int(number): dict(value)
            for number, value in (snapshot.get("surface_apertures") or {}).items()
        }
        self.stop_surface = snapshot.get("stop_surface", self.stop_surface)
        self.dimension = snapshot.get("dimension", self.dimension)
        self._modelled = snapshot.get("modelled", self._modelled)
        if "solves" in snapshot and self._modelled:
            self.solves = {int(number): name for number, name in snapshot["solves"].items()}
        if not self._custom_listing:
            self.listing = self._build_listing()

    def _vignetting_rows(self) -> list[str]:
        """VUX/VLX/VUY/VLY rows as LIS prints them: a row of zeros is left out."""
        rows = []
        for key in ("vux", "vlx", "vuy", "vly"):
            values = [float(item.get(key, 0.0)) for item in self.fields]
            if any(values):
                rows.append(f"    {key.upper()}       " + "      ".join(f"{value:.5f}" for value in values))
        return rows

    def _build_listing(self) -> str:
        header = "                RDY             THI     RMD       GLA           CCY   THC   GLC"
        def with_frozen_controls(row: str, solve: str = "100") -> str:
            return row.ljust(header.index("CCY")) + "100   " + solve
        if self._modelled:
            lines = [f"     {self.title}", header]
            for number in sorted(self.surfaces):
                surface = self.surfaces[number]
                label = ("> OBJ:" if number == 0 else "  IMG:" if number == max(self.surfaces)
                         else f"> STO:" if number == self.stop_surface else f"{number:>5}:")
                radius = "INFINITY" if surface["radius"] is None else f"{float(surface['radius']):.7g}"
                thickness = "INFINITY" if surface["thickness"] is None else f"{float(surface['thickness']):.7g}"
                lines.append(with_frozen_controls(
                    f"{label:<8} {radius:>15} {thickness:>15}       {surface['glass'] or ''}",
                    "PIM" if self.solves.get(number) == "PIM" else "100"))
            unit = {0: "IN", 1: "CM", 2: "MM"}[self.dimension]
            lines += ["", " SPECIFICATION DATA", f"    {self.aperture_kind.upper():<3}       {self.aperture_value:.5f}",
                      f"    DIM             {unit}",
                      "    WL          " + "    ".join(f"{row['nm']:.2f}" for row in self.wavelengths),
                      f"    REF              {self.reference}",
                      "    WTW         " + "    ".join("1" for _ in self.wavelengths),
                      "    XAN         " + "    ".join(f"{row['x']:.5f}" for row in self.fields),
                      "    YAN         " + "    ".join(f"{row['y']:.5f}" for row in self.fields),
                      "    WTF         " + "    ".join(f"{float(row.get('weight', 1.0)):.5f}" for row in self.fields),
                      *self._vignetting_rows(),
                      "", " REFRACTIVE INDICES",
                      *((" SOLVES", "    PIM") if self.solves else (" No solves defined in system",)),
                      " No pickups defined in system", "", " INFINITE CONJUGATES", "Command End:"]
            return "\r\n".join(lines) + "\r\n"
        lines = [f"     {self.title}", header]
        for number in sorted(self.surfaces):
            surface = self.surfaces[number]
            if number == 0:
                label = "> OBJ:"
            elif number == max(self.surfaces):
                label = "  IMG:"
            else:
                label = f"{number:>5}:"
            radius = "INFINITY" if surface["radius"] is None else f"{float(surface['radius']):.7g}"
            thickness = (
                "INFINITY" if surface["thickness"] is None else f"{float(surface['thickness']):.7g}"
            )
            glass = surface["glass"] or ""
            lines.append(with_frozen_controls(
                f"{label:<8} {radius:>15} {thickness:>15}       {glass}"))
        lines += [
            "",
            " SPECIFICATION DATA",
            f"    {self.aperture_kind.upper():<3}       {self.aperture_value:.5f}",
            "    DIM             MM",
            "    WL          656.30    587.60",
            "    REF              2",
            "    WTW              1         1",
            "    XAN        " + "    ".join(f"{float(row['x']):.5f}" for row in self.fields),
            "    YAN        " + "    ".join(f"{float(row['y']):.5f}" for row in self.fields),
            "    WTF        " + "    ".join(f"{float(row.get('weight', 1.0)):.5f}" for row in self.fields),
            *self._vignetting_rows(),
            "",
        ]
        if self.surface_apertures:
            lines += ["", " APERTURE DATA/EDGE DEFINITIONS", "    CA"]
            for number in sorted(self.surface_apertures):
                entry = self.surface_apertures[number]
                kind = str(entry.get("kind", "clear"))
                qualifier = "" if kind == "clear" else " " + {
                    "obscuration": "OBS",
                    "edge": "EDG",
                    "hole": "HOL",
                }.get(kind, kind.upper())
                label = f" L'{entry['label']}'" if entry.get("label") else ""
                lines.append(
                    f"    CIR S{number}{qualifier}{label}             {float(entry['radius']):.6f}"
                )
        lines += [
            "",
            " REFRACTIVE INDICES",
            " SOLVES",
            " No pickups defined in system",
            "",
        ]
        if self.zoom_positions > 1:
            lines += [" ZOOM DATA", "      " + "      ".join(
                f"POS {position}" for position in range(1, self.zoom_positions + 1)
            ), ""]
        lines += [
            " INFINITE CONJUGATES",
            "    EFL       100.0001",
            "    BFL        61.2346",
            "    FFL       -29.1234",
            "    FNO         2.0000",
            "    IMG DIS    61.2346",
            "    OAL        81.9134",
            "    PARAXIAL IMAGE",
            "     HT        23.4567",
            "    ANG        14.0000",
            "    ENTRANCE PUPIL",
            "     DIA       50.0000",
            "     THI       53.1234",
            "    EXIT PUPIL",
            "     DIA       56.1234",
            "     THI      -49.1234",
            "Command End:",
        ]
        return "\r\n".join(lines) + "\r\n"
