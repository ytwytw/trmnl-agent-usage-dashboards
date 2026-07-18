# Templates and Devices

## Template Matrix

| File | Canvas/use case |
| --- | --- |
| `templates/agent-usage-dashboard.liquid` | Full 800x480, color-capable renderer |
| `templates/agent-usage-dashboard-bwr.liquid` | Full 800x480, black/white/red-safe renderer |
| `templates/agent-usage-dashboard-bwr-half-horizontal.liquid` | Half-horizontal mashup |
| `templates/agent-usage-dashboard-bwr-half-vertical.liquid` | Half-vertical mashup |
| `templates/agent-usage-dashboard-bwr-quadrant.liquid` | Quadrant mashup |

Codex and Claude use the same template. The feed's `kind` field selects provider-specific sections.

## Variable Wiring

All shipped templates begin their data access with:

```liquid
{% assign s = source_1 %}
```

The expected wrappers are:

- Terminus Poll Extension: the first Exchange response is `source_1`.
- TRMNL SaaS Webhook: `trmnl-agent-usage-push` sends `merge_variables.source_1`.
- TRMNL SaaS Polling: a single response is normally exposed at the template root instead of `source_1`.

Webhook is the recommended SaaS path for a collector on a private network. If SaaS Polling is used with an
internet-reachable secured feed, adapt the first assignment to the root object exposed by the markup editor.

## Canvas and Padding

The full-size templates own an exact 800x480 canvas. Do not nest them inside a second padded 800x480 layout.

For TRMNL SaaS Private Plugins, set **Remove bleed margin?** to **Yes**. Imported plugin ZIP settings use:

```yaml
no_screen_padding: 'yes'
```

TRMNL expects quoted `yes`/`no` strings for this setting. Default wrapper padding can crop the right and bottom edges.

For Terminus, replace the generated template body with the complete selected template and select the correct model in
the Build Matrix. Preview the generated Screen before adding it to a Playlist.

## Device Support

The layouts are designed and visually tested for an 800x480 e-paper canvas. The primary physical test target is the
Seeed Studio reTerminal E1002. Other TRMNL/Terminus-compatible 800x480 devices should be able to reuse the feeds and
templates, subject to their renderer, palette, and firmware.

Non-800x480 devices are not first-class supported targets. A new target should be checked for:

- exact canvas and mashup dimensions;
- clipping and overflow with the longest schema-valid labels;
- palette mapping and dithering;
- e-paper contrast and minimum readable type;
- full-size and applicable mashup layouts;
- actual-device output, not browser preview only.

If another device needs an adapter, open an issue with its model, resolution, bit depth or palette, firmware/renderer
path, and sanitized screenshots. If the maintainer can source the hardware second-hand, or if a unit is donated or
sponsored, a device-specific template can be developed and physically QA'd.

## E1002 Color Path

Seeed documents E1002 as a full-color ACeP e-paper display, but its current TRMNL firmware guide notes that the TRMNL
firmware path renders E1002 content in monochrome mode. Use the BWR-safe full-size template unless the complete renderer
and firmware path is known to preserve color.

- [Seeed reTerminal E10xx hardware documentation](https://wiki.seeedstudio.com/reterminal_e10xx_main_page/)
- [Seeed TRMNL firmware guide](https://wiki.seeedstudio.com/reterminal_e10xx_trmnl/)

## Demo and Visual QA

The public fixtures are synthetic but follow the collector's real schema:

```text
examples/codex.sample.json
examples/claude.sample.json
```

Public demo screenshots must be rendered only from these fixtures or another reviewed synthetic fixture. Before
publishing an image, verify its dimensions, inspect it visually for clipping/overlap, and reject embedded text, EXIF, or
location metadata.

The repository demos use the full-size color template. Use the BWR-safe template when evaluating monochrome or
black/white/red firmware paths.
