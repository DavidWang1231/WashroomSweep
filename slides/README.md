# Slides

`WashroomSweep.pptx` — 15 slides, 16:9, with speaker notes on every slide.
Open in PowerPoint or Keynote, or upload to Google Drive and open as Google
Slides (fonts may substitute; the layout survives).

Four slides carry dashed placeholder boxes for photos or screen recordings:

| Slide | What goes there |
|---|---|
| 2 | A news screenshot, or a camera disguised as a hook / charger |
| 6 | The pilot measurement — photo or traffic capture |
| 8 | The live dashboard at the moment the alert fires |
| 9 | The dashboard mid-sweep or showing a verdict |

Delete the placeholder box and drop the image in its place.

## Regenerating

`build_deck.py` rebuilds the file from scratch, so edits to wording belong
there if you want them to survive a rebuild. Anything you add by hand in
PowerPoint — images, extra slides — will be lost on regeneration, so once
you start adding media, keep editing the .pptx directly.

```sh
../host/.venv/bin/python build_deck.py WashroomSweep.pptx
```

Needs `python-pptx` (`pip install python-pptx`).

## Figures worth checking before presenting

Everything quoted in the deck is measured, not estimated. The two that came
from outside our own bench are the Airbnb indoor-camera ban (April 2024) and
the South Korea inspection programmes — cite a source for those if a judge
asks. Market prices on slide 3 are approximate category ranges.
