# Devpost gallery

Three original 1920×1280 (3:2) PNG compositions, rendered locally with Playwright:

- `overlay-answer.png`: the actual overlay's frozen answered-card exit, with its
  question, exact answer and check stamp, over an original editor-like backdrop.
- `dock-queue.png`: the actual dock with three fictional viewers and an earlier
  answer chip. Its diamond prefix follows the application's glyph fallback.
- `architecture.png`: audio to Flux and turns back to the engine, hypotheses to
  the server, live UI state, EventSub in and threaded replies out.

Rebuild from the repository root:

```sh
uv run python docs/gallery/render.py
uv run pytest -m browser tests/test_gallery.py
```

`index.html` imports the shared theme directly. The renderer serves only local
files and replaces demo fixture data in memory; it does not change application
files, connect to providers, read credentials or send chat. The real overlay's
fixed demo step freezes its stamp. The dock uses its actual 400 px layout, scaled
only for this gallery composition. Names and text are fictional sample data.

All composition code, diagrams and the plain editor backdrop are original.
Fonts are local system fonts. No external images, logos, faces or font files.
