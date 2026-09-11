# Tuning and output contract

| Preset | Width cap | Palette | Contour epsilon | Merge passes |
| --- | ---: | ---: | ---: | ---: |
| preview | 768 | 96 | 0.38 px | 2 |
| balanced | 1200 | 160 | 0.30 px | 3 |
| faithful | 1600 | 256 | 0.24 px | 4 |

No upscaling is applied. The longest edge is also capped at twice the selected width cap.
`--max-width` accepts 1–2400; `--colors` accepts 2–256. Default output budget is 64 MiB;
`--max-output-mb` can change it. The budget is checked while accumulating shapes and again
before writing the final document. It is not a limit on peak RAM or computation time.
`--fit <MiB>` targets a size instead of failing at one: each attempt is a full render, and
the ladder deterministically steps width down by quarters to 512, then shrinks the palette
toward 256/64 width floors. Typical searches cost one extra render per halving; the report
records every attempt under `fit`, where failed attempts set `exceeded` and their `bytes`
mark the point where the budget tripped.

## Pipeline

1. Apply EXIF orientation, composite alpha onto the chosen matte, resize, and bilateral-filter.
2. Median-cut color quantization in perceptual Oklab space, without dithering;
   palette entries are the sRGB means of their Oklab buckets.
3. Merge components smaller than 16 pixels only into adjacent, larger, similar-color regions.
   Maximum per-channel color distance is 18; cumulative drift from original labels is 23.
4. Trace contours at 4x sampling. Slightly dilate, smooth and simplify them at subpixel scale.
5. Preserve holes and separate islands using even-odd polygons with retraced zero-area bridges.
6. Fit local linear color gradients in larger regions; group tiny shapes by palette and tile.
7. Add clipped, lower-resolution color underpainting beneath the detail contours to reduce
   pale antialiasing seams when the composition is displayed at small sizes.
8. Write one responsive HTML document and audit its restricted structure.

## Practical limits

- Best suited to illustrations with coherent color regions. Photographs, noise and dense
  texture can generate large documents; reduce resolution or use a smaller palette.
- This is a lossy approximation. Fine lines, subtle shading, silhouettes and antialiasing
  may differ from the reference. Colors within three channel values of the matte are omitted.
- Output contains one `main`, many `div` layers, inline CSS and a restrictive CSP. No image
  files, embedded SVG, Canvas, scripts, fonts, base64, external resources or pixel grids.
- Python dependencies are authoring-time tools. They are not needed to open the resulting HTML.
- Browsers must support `clip-path: polygon(evenodd, ...)`, gradients and `aspect-ratio`.
  Current Chromium is the primary visual verification target; check other target browsers.
- Thousands of polygons may be expensive on low-memory devices. Preview both viewport sizes.
- PNG transparency is flattened; animated/multipage inputs are rejected. Embedded ICC profiles
  are not color-managed; convert unusual wide-gamut/CMYK sources to sRGB for predictable color.
- Identical input/settings reproduce identical HTML in the same dependency environment.
  Different Pillow/OpenCV/NumPy versions can change quantization or floating-point fitting.
- The audit checks this generator's dialect. It is not a sanitizer for arbitrary hostile HTML
  and does not measure visual similarity. Treat browser QA as a separate required step.
- `--score` rasterizes the emitted shapes offline (solids and linear gradients) and writes
  `similarity.mae` and `similarity.mae_thumbnail` (mean absolute error, 0-255, lower is
  better) to the report. It is an authoring-time estimate of the approximation, not a
  substitute for viewing the result in a browser.
- The MIT license covers this project's code and original fixtures, not rights to input artwork.
