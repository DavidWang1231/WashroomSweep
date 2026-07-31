# Slides

Eight slides: intro · background · what we built · WiFi demo · camera demo ·
BLE · extensions · summary.

| File | Use |
|---|---|
| `WashroomSweep.pdf` | Present from this. Preview, full screen (⌘⇧F), arrow keys. Works anywhere. |
| `WashroomSweep.pptx` | Editable, for sharing. Opens in PowerPoint and Google Slides. **Keynote will not open it** — that's a known Keynote limitation with generated files, not a broken file. |
| `deck.html` | Same deck in a browser. Press **N** for Chinese speaker notes. |
| `build_deck.py` | Regenerates the .pptx. |

## Adding photos

Easiest in the .pptx — drag an image onto any slide.

To put one in the PDF instead, edit `deck.html` (a plain
`<img src="photo.jpg">` beside the file works) and re-render:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --no-pdf-header-footer --print-to-pdf=WashroomSweep.pdf file://$PWD/deck.html
```

Regenerating the .pptx overwrites hand-added images, so once you start adding
media there, edit the .pptx directly and leave `build_deck.py` alone.

## Before presenting

Every figure quoted is from our own bench. The two outside facts — Airbnb's
April 2024 indoor-camera ban and South Korea's inspection programmes — deserve
a citation if a judge asks, and the market prices on slide 2 are approximate
category ranges.
