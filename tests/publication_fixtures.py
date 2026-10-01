"""Hand-authored synthetic AUT records; no CODE V, vendor samples or private files."""
from __future__ import annotations


def aut_report() -> dict:
    fields = [{"number": n + 1, "x_angle": 0, "y_angle": n * 3, "weight": 1.0,
               "vux": 0.0, "vlx": 0.0, "vuy": 0.4 if n == 2 else 0.0, "vly": 0.0}
              for n in range(3)]
    wave = {"state": "succeeded", "result": {"weighted_rms_waves": 0.2, "weighted_strehl": 0.6,
            "fields": [{"field_number": n + 1, "rms_waves": 0.1 * (n + 1), "strehl": 0.6,
                        "rays_traced": count} for n, count in enumerate((900, 800, 600))]}}
    order = {"effective_focal_length": 100, "back_focal_length": 40, "f_number": 4,
             "overall_length": 10, "image_distance": 40}
    return {"state": "complete", "stages": [
        {"index": n, "name": f"grow-{n}", "status": "succeeded", "constraints": [],
         "constraints_satisfied": True, "bounds_satisfied": True} for n in (1, 2, 3)],
        "spec": {"stages": [{"error_function": {"DEL": 0.01, "WTA": 1}}]},
        "initial_error": 200, "final_error": 100, "all_constraints_satisfied": True,
        "final_constraints_satisfied": True, "explicit_bounds_satisfied": True,
        "before_first_order": dict(order), "after_first_order": dict(order),
        "before_wavefront": wave, "after_wavefront": wave,
        "candidate_snapshot": {"zooms": [{"fields": fields}], "aperture_kind": "epd", "aperture_value": 8,
                               "wavelengths": [{"micrometers": 0.55, "weight": 1}], "reference_wavelength": 1}}


def aut_record() -> dict:
    commands = []
    for n in (1, 2, 3):
        commands += [f"! stage {n} (synthetic): succeeded", "yan f2 6", "yan f3 9", "wtf f3 2",
                     "frz s0..i", "ccy s1 0", "aut", "err cdv", "mxc 0", "vli y", "go",
                     "aut", "err cdv", "mxc 15", "mnc 1", "efl = 100", "tim 60", "vli y",
                     f"out stage-{n}.lis", "go", "out t", "ccy s1 100", f"sav stage-{n}.len"]
    return {"steps": [{"action": "aut", "native_commands": commands, "results": aut_report()}]}
