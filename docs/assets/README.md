# README visual assets

`orchkit-hero.svg` is an original, repository-local OrchKit banner. Its navy grid, violet/amber/green accents, and workflow cards follow the visual direction of the owner's Superkit repository without importing its model claims, counts, or runtime behavior.

[`orchkit-workflow.svg`](orchkit-workflow.svg) is the canonical diagram of the implemented workflow in the README. Its eight phases cover operator task definition, local task authority, a fresh ChatGPT attempt, scoped edits, result submission and quiesce, local verification, required gates, and completion or guarded publication. The repair path returns to a fresh attempt with durable feedback from local state.

## Maintenance

- Keep both graphics as UTF-8 SVG with a `viewBox`, accessible title/description, and descriptive README alternative text.
- Keep them self-contained: no scripts, event handlers, animation, external fonts, remote images or URLs, CSS imports, or `foreignObject`.
- Keep the wording aligned with implemented workflow boundaries. A worker receipt is not verification; publication remains gated; the core is not an OS sandbox.
- Do not add future Bridge, macOS app, or multi-session features to the workflow diagram until they ship.
- Use phase numbers, labels, and clear connectors to communicate meaning; colors are supplemental.
- Do not put release availability, transient CI results or run numbers, test counts, or private run/device information into either static graphic. Live/status information belongs in linked badges and the README status section.
- Preserve the hero banner's 1280 × 520 aspect ratio. The workflow diagram has its own canvas; preserve readable labels, a clear main path, and a visible fresh-attempt repair loop at roughly 900–1000 px README width. Textual documentation remains the usable alternative on narrow screens or when images are disabled.
- Render-check both graphics after changes with a local native renderer, then inspect for clipping, overlaps, broken connectors, and unreadable text.
- Validate SVG parsing, local image references, and Markdown links before publication. Rendering previews are review evidence, not additional package artifacts.

The four static badges are repository-local SVGs in a bold, rectangular style with a shared dark label color. Their text is the status; color is supplementary. Live CI uses GitHub's own workflow badge, not a static success graphic. The header does not depend on an external badge-generation service. CI links to the real workflow, the version badge says **source**, and the license remains **Apache-2.0**. Do not change these into unsupported release/support claims for visual symmetry.
