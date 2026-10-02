# Design Specification Format (schema 1)

[中文](design-spec.md)

A design specification is a JSON file supplied by the user that describes the system definition, evaluation configuration, criteria presets and per-item requirements a design must meet. It drives the analysis selection of the [evaluation package](../comparison-workflow.en.md#design-specification-evaluation-package) and the [specification judgement](#specification-judgement). The code has no course or project defaults: thresholds, glass catalogs and frequencies all come from the specification file, and the only built-in items are criteria presets with stated sources that must be selected explicitly.

Example: [`design-spec-dbgauss.json`](design-spec-dbgauss.json), for the three-field test model of the simulated backend (the old example file name is kept). **The system definition in the example is a hand-written demonstration condition, and every threshold is a demonstration value, not a design requirement.** Thresholds for a real task are filled in by the user; write unknown items as pending instead of guessing.

## Top-level fields

| Field | Required | Description |
| --- | --- | --- |
| `schema_version` | Yes | Always `1` |
| `kind` | Yes | Always `"design_spec"` |
| `name` | Yes | Non-empty name |
| `description` | No | Description text |
| `demonstration` | No | `true` means the thresholds are demonstration values; the judgement result carries this flag unchanged |
| `units` | Yes | `mm`, `cm` or `inch`; must match the evaluated lens, and the unit of length requirements must equal it too |
| `system` | Yes | System definition, see the next section; may be an empty object |
| `evaluation` | Yes | Evaluation configuration: `analyses`, `mtf_frequencies`, `spot_grid` |
| `criteria` | Yes | List of explicitly selected criteria presets; may be empty |
| `requirements` | Yes | List of per-item requirements; may be empty |

Unknown fields are always refused, so that a misspelling is not silently ignored. Any item can carry `source` text (task wording, page number, "pending" and so on), which the judgement result keeps unchanged.

## System definition `system`

All four items can be omitted or `null` (not checked). Written as `{"pending": true, "source": "pending"}`, an item is judged unknown; a pending item cannot give values at the same time.

| Item | Value fields | How it is judged |
| --- | --- | --- |
| `aperture` | `kind` (`epd`/`fno`/`na`/`nao`), `value` | The defined value is compared only when the lens aperture type is the same; a different type returns unknown, and a requirement such as `f_number` with a tolerance should be used instead |
| `fields` | `values`: `[{x_angle?, y_angle, weight?}]`, angles in degrees | Count, angles (tolerance 1e-6 degrees) and any given weights match item by item |
| `wavelengths` | `values`: `[{nm, weight?}]`, `reference` (1-based index or `null`) | Count, wavelengths (tolerance 1e-6 nm), weights and reference wavelength match |
| `glass_catalogs` | `allowed`: list of catalog names, for example `["SCHOTT"]` | Judged from the last part of the lens glass name `name_catalog`; a catalog not in the list is fail, and a name without a catalog suffix is unknown |

A system definition mismatch means the lens being evaluated is not the system the specification describes, so these entries all count as required items in the overall judgement.

## Evaluation configuration `evaluation`

- `analyses`: a non-empty subset of `first_order`, `spot_diagram`, `mtf`, `wavefront`, `native_plot`. Specification evaluation always uses all of the lens's fields and wavelengths and is limited to single-zoom lenses.
- `mtf_frequencies`: ascending, unique, non-negative frequencies in cycles/mm, at most 101, used for the structured diffraction MTF. The native MTF plot is still fixed at `MFR 100; IFR 10`.
- `spot_grid`: spot plotting grid, 2 to 101.

The analyses a requirement refers to must be in `analyses`: MTF requirements need `mtf` with the frequency in the list; spot and WAV requirements need the corresponding analysis; distortion requirements need `native_plot` (read from the FIE text of the `field_aberration` native plot); thickness requirements only read lens data and need no analysis.

## Criteria presets `criteria`

A preset takes effect only when listed, and expands into one judgement per lens field:

| Preset | Options | Content and source |
| --- | --- | --- |
| `marechal` | `required` | Maréchal criterion: WAV RMS wavefront error ≤ 0.07 waves and CODE V Strehl ≥ 0.8. λ/14 ≈ 0.0714 λ corresponds to Strehl ≈ 0.8 (Born & Wolf, *Principles of Optics*, section 9.3); the preset uses the common, slightly stricter 0.07 |
| `airy_spot` | `statistic` (`rms` or `geometric_max`), `required` | Spot **diameter** (2 × native SPO radius) ≤ Airy disk diameter 2.44 λ F/# (Born & Wolf 8.5.2). λ is the lens reference wavelength and F/# the CODE V first-order F/#; the Airy disk is a value computed by the service. Infinite object distance only |

## Per-item requirements `requirements`

Each requirement must give all nine of these keys:

```json
{"id": "efl", "metric": "effective_focal_length", "unit": "mm",
 "field": null, "direction": null, "frequency": null,
 "minimum": 99.5, "maximum": 100.5, "required": true,
 "source": "task text, page 2"}
```

The specification additionally allows `source`, `note` and `pending`. An item with `pending: true` must have both limits `null` and is judged unknown ("pending"). IDs must be unique, and the `system.` and `preset.` prefixes are reserved for system conditions and presets.

| metric | Unit | Conditions | Value source |
| --- | --- | --- | --- |
| `effective_focal_length`, `overall_length`, `back_focal_length` | Specification length unit | None | First-order analysis; `OAL` runs from surface 1 to the last lens surface and **excludes the image distance**; the back focal distance takes only the BFL printed in the listing |
| `f_number` | `ratio` | None | First-order analysis |
| `mtf` | `ratio` | `field`, `direction` (`tangential`/`sagittal`), `frequency` | Structured diffraction MTF |
| `spot_rms_radius`, `spot_max_radius` | Length unit | `field` | Native SPO statistics, radius |
| `wavefront_rms_waves`, `wavefront_strehl` | `waves`, `ratio` | `field` | WAV at the current focus |
| `distortion_max_abs` | `percent` | None | Maximum absolute value among the 11 sample points of the FIE reference-wavelength distortion table |
| `center_thickness_min`, `center_thickness_max` | Length unit | None | Glass center thickness (lens read-back) |
| `edge_thickness_min` | Length unit | None | Glass edge thickness, computed by the service |
| `air_center_thickness_min`, `air_edge_thickness_min` | Length unit | None | Air gap center/edge thickness; edge computed by the service |

## Specification judgement

Each result is `pass`, `fail` or `unknown`, with the value, unit, precision note, `value_source` (`codev` or `service_calculated`) and `threshold_source` (`spec`, `preset:marechal` or the Airy disk computed by the service). Overall judgement: any required item that fails gives fail; pass only when there are required items and all of them pass; anything else is unknown. Missing data, a simulated source and pending thresholds never count as passing.

- **Print precision**: back focal distance, spot, WAV and distortion come from printed text and get a conservative half-last-digit interval based on the decimals kept; a threshold inside the interval gives unknown. Spot intervals are derived from the printed diameters.
- **Machine precision**: first-order `EFL`, F/#, `OAL` and thicknesses are read with `EvaluateExpression`, about 16 significant digits, and CODE V's internal arithmetic adds rounding of about 1e-15 (a thickness pushed by optimization to a lower bound of 0.1 reads back as 0.09999999999999896). When judging, thresholds are widened by 1e-14 × max(|value|, 1) (lens units); a value that misses only within this rounding is judged pass as "met", and the reason says so; `tolerance` in the result gives the width used, and a real violation is still fail. Printed quantities (back focal distance, spot, WAV, distortion) do not get this width and still use only the print-precision interval.
- **Distortion**: parses the default rotationally symmetric table of `FIE;LSA;GO` (reference wavelength, relative field 0.0 to 1.0 in 11 points, angles in degrees). Only Y angle fields are accepted; an FIE full-field angle that does not match the lens maximum field, a wavelength that is not the reference wavelength, a missing table or a different format (for example separate chromatic tables or a full-field display) all return unknown. CODE V computes distortion against the ideal (paraxial) image height, and the maximum is taken over the sample points only.
- **Thickness**: a glass segment runs from each surface followed by glass to the next surface (two segments for a cemented doublet); an air segment lies between adjacent surfaces that "touch glass", skipping dummy surfaces with air on both sides (for example a standalone stop surface). Object and image distances are excluded. The edge height is the larger of the `GetMaxAperture` effective maximum semi-apertures of the two end surfaces, computed with spherical sag; this differs from the ray-defined edge used by MNE/MAE in CODE V AUT (the length of the highest ray on the first surface projected onto the optical axis), so the values may disagree. Edge thickness is computed only when the native `LIS` surface section confirms there is no special surface data; with reflecting surfaces, multi-zoom, missing semi-apertures, or an edge height larger than a surface's spherical radius (a real part would need a bevel/flat), it is unknown, and the reason gives the surface number and values. When the edge thickness of one element cannot be computed, the minimum of the other elements must not stand in for it.
- **Result fields**: thickness and distortion items list per-segment or per-point data in `details`, to make it easy to find the element that is constraining.

## Input validation

The specification is read before CODE V starts; when the lens units do not match the specification, a requirement refers to a field that does not exist, or the lens is multi-zoom, the run is refused after preflight and before analysis.
