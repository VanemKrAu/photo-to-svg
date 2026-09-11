"""CLI orchestration; heavyweight image dependencies load only for conversion."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from . import __version__
from .audit import audit_file, audit_html


PRESETS = {
    "preview": {"max_width": 768, "colors": 96, "epsilon": .38, "passes": 2},
    "balanced": {"max_width": 1200, "colors": 160, "epsilon": .30, "passes": 3},
    "faithful": {"max_width": 1600, "colors": 256, "epsilon": .24, "passes": 4},
}


def integer_between(low, high):
    def convert(value):
        result = int(value)
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"Expected an integer from {low} to {high}")
        return result
    return convert


def positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Expected a finite positive number")
    return result


def rgb_hex(value):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError("Expected #RRGGBB, for example #ffffff")
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def build_parser():
    parser = argparse.ArgumentParser(description="Convert a local raster reference into standalone HTML/CSS contour art.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("convert", help="Offline trace; the output has no images, scripts or external assets")
    convert.add_argument("input", type=Path)
    convert.add_argument("-o", "--output", type=Path, required=True)
    convert.add_argument("--preset", choices=PRESETS, default="faithful")
    convert.add_argument("--max-width", type=integer_between(1, 2400), help="Trace width cap (also caps longest edge at 2x this value)")
    convert.add_argument("--colors", type=integer_between(2, 256))
    convert.add_argument("--background", type=rgb_hex, default=(255, 255, 255), help="CSS matte and transparency composite color; default #ffffff")
    convert.add_argument("--title", default="CSS contour illustration", help="Accessible description; escaped as text")
    convert.add_argument("--no-gradients", action="store_true")
    convert.add_argument("--no-underpainting", action="store_true")
    convert.add_argument("--max-output-mb", type=positive_float, default=64, help="HTML size budget in MiB (default 64)")
    convert.add_argument("--fit", type=positive_float, help="Target size in MiB; automatically step down width/colors until the HTML fits")
    convert.add_argument("--score", action="store_true", help="Rasterize the shapes offline and report MAE against the reference")
    convert.add_argument("--report", type=Path, help="Optional machine-readable JSON report")
    convert.add_argument("--force", action="store_true", help="Replace existing output/report files")
    convert.add_argument("--quiet", action="store_true", help="Suppress progress on stderr; retain JSON result on stdout")
    audit = sub.add_parser("audit", help="Check the static HTML/CSS generator contract; no image dependencies needed")
    audit.add_argument("input", type=Path)
    return parser


def check_paths(source, output, report, force):
    paths = [source.resolve(), output.resolve()]
    if report is not None:
        paths.append(report.resolve())
    if len(set(paths)) != len(paths):
        raise ValueError("Input, output and report must be different files.")
    if output.suffix.lower() != ".html":
        raise ValueError("Output must have the .html extension.")
    for target in (output, report):
        if target is not None and target.exists() and not force:
            raise ValueError(f"File already exists: {target}. Use --force to replace it.")


def atomic_write(path: Path, text: str, force: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        if force:
            os.replace(temporary, path)
        else:
            try:
                # Hard-link installation is atomic and refuses a concurrent overwrite.
                os.link(temporary, path)
            except OSError:
                if path.exists():
                    raise
                # Filesystems without hard links (FAT32, some network shares) fall
                # back to exclusive creation, which still refuses an existing file.
                with open(path, "xb") as target:
                    target.write(temporary.read_bytes())
        temporary.unlink(missing_ok=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fit_candidates(config):
    """Deterministic fallback ladder: shrink width first, then the palette."""
    width, colors = config["max_width"], config["colors"]
    yield dict(config)
    while width > 512:
        width = max(512, round(width * 3 / 4 / 4) * 4)
        yield {**config, "max_width": width}
    for width_floor, divisor in ((512, 2), (384, 4), (300, 8), (256, 16)):
        shrunk = max(2, min(colors, colors // divisor))
        yield {**config, "max_width": min(config["max_width"], width_floor), "colors": shrunk}


def convert(args):
    check_paths(args.input, args.output, args.report, args.force)
    import cv2
    import numpy as np
    from PIL import __version__ as pillow_version
    from .regions import load_reference, merge_regions, quantize
    from .render import BudgetExceeded, render_document

    cv2.setNumThreads(1)
    cv2.setRNGSeed(0)
    start = time.perf_counter()
    config = dict(PRESETS[args.preset])
    for name in ("max_width", "colors"):
        if getattr(args, name) is not None:
            config[name] = getattr(args, name)
    progress = (lambda _: None) if args.quiet else (lambda message: print(message, file=sys.stderr, flush=True))
    limit = int(args.max_output_mb * 1024 * 1024)
    target = limit if args.fit is None else min(limit, int(args.fit * 1024 * 1024))
    attempts = []
    document = stats = reference = original = None
    last = dict(config)
    previous = None
    candidates = fit_candidates(config) if args.fit is not None else iter((dict(config),))
    for candidate in candidates:
        key = (candidate["max_width"], candidate["colors"])
        if key == previous:
            continue
        previous = key
        last = candidate
        reference, original = load_reference(args.input, candidate["max_width"], args.background)
        labels, palette = quantize(reference, candidate["colors"])
        labels = merge_regions(labels, palette, candidate["passes"], progress)
        try:
            document, stats = render_document(
                reference, labels, palette, original, background=args.background, title=args.title,
                epsilon=candidate["epsilon"], gradients=not args.no_gradients,
                underpainting=not args.no_underpainting,
                max_bytes=target, progress=progress, score=args.score,
            )
        except BudgetExceeded as exceeded:
            if args.fit is None:
                raise
            attempts.append({"max_width": candidate["max_width"], "colors": candidate["colors"],
                             "bytes": exceeded.byte_count, "exceeded": True})
            progress(f"Fit attempt {len(attempts)}: width {candidate['max_width']}, colors "
                     f"{candidate['colors']} exceeded the target; stepping down")
            continue
        audit = audit_html(document)
        if not audit["valid"]:
            raise ValueError("Generated HTML failed audit: " + "; ".join(audit["errors"]))
        attempts.append({"max_width": candidate["max_width"], "colors": candidate["colors"],
                         "bytes": audit["bytes"]})
        config = candidate
        break
    else:
        raise ValueError(f"Cannot fit the illustration within {target / 1024 / 1024:.2f} MiB even at "
                         f"width {last['max_width']} and {last['colors']} colors; raise --fit/--max-output-mb.")
    audit = audit_html(document)
    report = {
        "version": __version__, "preset": args.preset, "settings": config,
        "original_size": list(original), "trace_size": list(reference.shape[1::-1]),
        **stats, "bytes": audit["bytes"], "sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
        "audit": audit, "seconds": round(time.perf_counter() - start, 3),
        "dependencies": {"numpy": np.__version__, "opencv": cv2.__version__, "pillow": pillow_version},
    }
    if args.fit is not None:
        report["fit"] = {"target_mb": args.fit, "attempts": attempts}
    atomic_write(args.output, document, args.force)
    if args.report:
        atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n", args.force)
    return report


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = convert(args) if args.command == "convert" else audit_file(args.input)
    except (OSError, ValueError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # cv2.error subclasses Exception directly and cv2 is imported only inside
        # convert, so oversized or pathological inputs cannot be named here.
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("valid", True) else 1
