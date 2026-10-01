"""Numeric comparison projection, retaining conventions and precision."""

def metrics(result: dict, kind: str) -> dict:
    if kind not in {"first_order", "spot_diagram", "mtf", "wavefront", "native_plot"}:
        raise ValueError(kind)
    # Keep every numeric value, convention and precision note; only paths,
    # image transport and diagnostic text vary with task/session identifiers.
    return {key: value for key, value in result.items()
            if key not in {"image", "raw_output", "warnings", "plot_file_path", "plot_file_bytes"}}


