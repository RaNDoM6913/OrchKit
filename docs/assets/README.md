# README visual assets

`orchkit-hero.svg` is an original, repository-local OrchKit banner. Its navy grid, violet/amber/green accents, and workflow cards follow the visual direction of the owner's Superkit repository without importing its model claims, counts, or runtime behavior.

## Maintenance

- Keep the graphic as UTF-8 SVG with a `viewBox`, accessible title/description, and descriptive README alternative text.
- Keep it self-contained: no scripts, animation, external fonts, remote images, or `foreignObject`.
- Keep the wording aligned with implemented workflow boundaries. A worker receipt is not verification; publication remains gated; the core is not an OS sandbox.
- Do not put release availability, live CI results, test counts, or private run/device information into a static banner. Live/status information belongs in linked badges and the README status section.
- Preserve the 1280 × 520 aspect ratio and preview at typical README width after changes. Textual documentation remains the usable alternative on narrow screens or when images are disabled.
- Validate SVG parsing, local image references, and Markdown links before publication. Rendering previews are review evidence, not additional package artifacts.

The four static badges are repository-local SVGs in a bold, rectangular style with a shared dark label color. Their text is the status; color is supplementary. Live CI uses GitHub's own workflow badge, not a static success graphic. The header does not depend on an external badge-generation service. CI links to the real workflow, the version badge says **source**, and the license remains **Apache-2.0**. Do not change these into unsupported release/support claims for visual symmetry.
