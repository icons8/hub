# Third-party notices

The code of Hub by Icons8 is licensed under the Apache License 2.0. This repository also contains the
third-party components listed below. Each keeps its own license, which is not changed by the license of the hub. The
files are named in the "Where" column.

## Summary

| Component | Version | License | Where |
| --- | --- | --- | --- |
| marked | 15.0.7 | MIT | `modules/web/vendor/marked.min.js` |
| highlight.js | 11.11.1 | BSD-3-Clause | `modules/web/vendor/highlight.min.js` |
| DOMPurify | 3.4.16 | Apache-2.0 (dual-licensed: Apache-2.0 or MPL-2.0; the hub uses it under Apache-2.0) | `modules/web/vendor/purify.min.js` |
| Mermaid | 11.17.2 | MIT, with the bundled packages listed below | `modules/web/vendor/mermaid.min.js` |
| Onest | n/a (copyright 2021) | SIL OFL 1.1 | `modules/web/static/fonts/onest-*.woff2` |
| Geist Mono | n/a (copyright 2024) | SIL OFL 1.1 | `modules/web/static/fonts/geist-mono-*.woff2` |
| Material Icons: `dashboard`, `mic` | n/a | Apache-2.0 | symbols `i-board` and `i-mic` in `modules/web/static/icons.svg` |
| Clean (the page's default theme) | 0.1.0, commit 1861631 | MIT | `modules/web/static/clean/` (`clean.css`, `clean.js`, `rows.js`, `fit.js`, `usage.js`; the license is `LICENSE` there) |
| Clean characters (AI-generated) | n/a | Apache-2.0 | `modules/web/static/clean/a/*.png` |
| Geist | n/a (copyright 2024) | SIL OFL 1.1 | `modules/web/static/clean/f/GeistVariable.woff2` |
| Lora (Bold) | n/a (copyright 2011) | SIL OFL 1.1 | `modules/web/static/clean/lora-700-*.woff2` |
| Emoji names (CLDR annotations) | 49.0.0 | Unicode License v3 | `modules/web/static/emoji/*.json` (the license text is `UNICODE-LICENSE.txt` there) |
| Icons8 icons | n/a | Icons8 LLC, distributed with its permission as part of Hub by Icons8 (not Apache-2.0) | below |

## Material Icons

The `dashboard` (outlined) and `mic_none` glyphs are from Material Icons by Google
(https://github.com/google/material-design-icons), licensed under the Apache License 2.0. They are used as the `i-board`
and `i-mic` symbols.

## Fonts

Onest (Copyright 2021 The Onest Project Authors, https://github.com/simpals/onest) and Geist Mono (Copyright 2024 The Geist
Project Authors, https://github.com/vercel/geist-font) are licensed under the SIL Open Font License, Version 1.1, Latin,
Latin Extended and Cyrillic subsets, variable weight, WOFF2. The license text is in
`modules/web/static/fonts/OFL.txt`, next to the fonts.

## Clean

The Clean theme, the default look of the page: Copyright (c) 2026 Ilnur Kalimullin
(https://github.com/kalimullin/hub-plugin-clean, commit 1861631), MIT; the license text is in
`modules/web/static/clean/LICENSE`. Changed for the core: comments in English, strings from the hub's catalogs, logical
CSS properties.

Its characters, the 24 session pictures and the Hub's own head `modules/web/static/clean/a/*.png`, are AI-generated
images made for the theme by its author, who gives them under the Apache License 2.0, as the hub.

Its fonts: Geist (Copyright 2024 The Geist Project Authors, https://github.com/vercel/geist-font) and Lora (Copyright
2011 The Lora Project Authors, https://github.com/cyrealtype/Lora-Cyrillic), SIL Open Font License 1.1; the license
texts are next to them, `modules/web/static/clean/f/Geist-OFL.txt` and `modules/web/static/clean/f/Lora-OFL.txt`.

## Emoji names

The names and keywords of the emoji in the session icon search, `modules/web/static/emoji/*.json`, one file per
language, are Unicode CLDR annotations (cldr-json 49.0.0, `annotations` and `annotationsDerived`): Copyright ©
2015-2024 Unicode, Inc., licensed under the Unicode License v3 (SPDX: Unicode-3.0), which permits use, copying,
modification and distribution when the copyright and permission notice goes with the data. The notice is
`modules/web/static/emoji/UNICODE-LICENSE.txt`, next to the files. The files are rebuilt by `tools/emoji_names.py`: the
selection and the order of the emoji follow the `emoji-test.txt` of Emoji 16.0 (only which emoji, no text of it is
copied), the names are CLDR's as they are.

## Icons8 icons

The icons listed here are by Icons8 LLC. They are not covered by the Apache License 2.0 of the hub: Icons8 LLC
permits distributing them as part of Hub by Icons8, unchanged in content, with this notice. Using them outside Hub by Icons8
is subject to the Icons8 license terms.

- The `m_outlined` symbols of `modules/web/static/icons.svg`, except `i-board` and `i-mic` (Material Icons, above); the
  list with their Icons8 ids is `modules/web/icons8.json`.
- The symbols of `modules/files/static/icons.svg` (the same pack; their Icons8 ids are in the comment at the top of the file).
- The Forma Regular icons of the Clean theme: `modules/web/static/clean/*.svg`, `modules/web/static/clean/i/` and
  `modules/web/static/clean/id/`; the list with their Icons8 ids is `modules/web/static/clean/icons8.json`.
  `sidebar.svg` is drawn by the theme's author in the same style and goes with the theme under MIT.

The hub's mark (the sun of dots) is not one of them: it is the project's own work under the Apache License 2.0, like the
code — `modules/web/favicon.svg`, `modules/web/static/favicon/`, `media/logo.svg`, the app icons of
`modules/web/static/icons/` and the Mac launcher's `logo.icns`.

## Bundled JavaScript libraries

- **marked** 15.0.7: Copyright (c) 2011-2025, Christopher Jeffrey; Copyright (c) 2018+, MarkedJS. MIT, text below.
- **highlight.js** 11.11.1: Copyright (c) 2006, Ivan Sagalaev; (c) 2006-2024 Josh Goebel and other contributors.
  BSD-3-Clause, text below.
- **DOMPurify** 3.4.16: Copyright (c) Cure53 and other contributors. Apache-2.0 or MPL-2.0, the hub uses it under Apache-2.0.
  The same applies to the copy bundled inside Mermaid (3.4.12).
- **Mermaid** 11.17.2: Copyright (c) 2014-2022 Knut Sveidqvist. MIT, text below.
  `mermaid.min.js` is the unmodified `dist/mermaid.min.js` of the `mermaid@11.17.2` npm package; it is a single bundle
  (esbuild) with its dependencies inside and no license header of its own.

### Packages bundled inside `mermaid.min.js`

The list comes from the source map of that file (so only packages that really are in the bundle, with the versions
that were bundled). Several packages appear in two versions because Mermaid's dependencies pin older ones. The
license text for each group follows after the tables; the holders are as named in each package's own license file.

#### MIT

| Package | Version | Copyright holders |
| --- | --- | --- |
| @braintree/sanitize-url | 7.1.2 | Braintree |
| @iconify/utils | 3.0.2 | Vjacheslav Trushkin |
| @upsetjs/venn.js | 2.0.0 | Ben Frederickson; Samuel Gratzl |
| cose-base | 1.0.3 | iVis@Bilkent |
| cose-base | 2.2.0 | iVis@Bilkent |
| cytoscape | 3.34.0 | The Cytoscape Consortium |
| cytoscape-cose-bilkent | 4.1.0 | The Cytoscape Consortium |
| cytoscape-fcose | 2.2.0 | iVis-at-Bilkent |
| dagre-d3-es | 7.0.14 | Chris Pettitt (dagre, dagre-d3, graphlib); Thibaut Lassalle, David Newell, Alois Klink, Sidharth Vinod and dagre-es contributors |
| dayjs | 1.11.21 | iamkun |
| es-toolkit | 1.45.1 | Viva Republica, Inc; OpenJS Foundation and other contributors |
| fastdom | 1.0.12 | Wilson Page |
| js-yaml | 4.3.0 | Vitaly Puzrin |
| katex | 0.16.47 | Khan Academy and other contributors |
| khroma | 2.1.0 | Fabio Spampinato, Andrew Maney |
| layout-base | 1.0.2 | iVis@Bilkent |
| layout-base | 2.0.1 | iVis@Bilkent |
| lodash-es | 4.18.1 | OpenJS Foundation and other contributors; Jeremy Ashkenas, DocumentCloud and Investigative Reporters & Editors (Underscore.js) |
| marked | 16.3.0 | MarkedJS; Christopher Jeffrey; John Gruber (markdown.pl) |
| roughjs | 4.6.6 | Preet Shihn |
| stylis | 4.3.6 | Sultan Tarimo |
| ts-dedent | 2.2.0 | Tamino Martinius |
| uuid | 14.0.0 | Robert Kieffer and other contributors |

#### ISC

| Package | Version | Copyright holders |
| --- | --- | --- |
| d3 | 7.9.0 | Mike Bostock |
| d3-array | 3.2.4 | Mike Bostock |
| d3-axis | 3.0.0 | Mike Bostock |
| d3-brush | 3.0.0 | Mike Bostock |
| d3-chord | 3.0.1 | Mike Bostock |
| d3-color | 3.1.0 | Mike Bostock |
| d3-contour | 4.0.2 | Mike Bostock |
| d3-delaunay | 6.0.4 | Observable, Inc; Mapbox |
| d3-dispatch | 3.0.1 | Mike Bostock |
| d3-drag | 3.0.0 | Mike Bostock |
| d3-dsv | 3.0.1 | Mike Bostock |
| d3-fetch | 3.0.1 | Mike Bostock |
| d3-force | 3.0.0 | Mike Bostock |
| d3-format | 3.1.0 | Mike Bostock |
| d3-geo | 3.1.1 | Mike Bostock; Charles Karney |
| d3-hierarchy | 3.1.2 | Mike Bostock |
| d3-interpolate | 3.0.1 | Mike Bostock |
| d3-path | 3.1.0 | Mike Bostock |
| d3-polygon | 3.0.1 | Mike Bostock |
| d3-quadtree | 3.0.1 | Mike Bostock |
| d3-random | 3.0.1 | Mike Bostock |
| d3-scale | 4.0.2 | Mike Bostock |
| d3-scale-chromatic | 3.1.0 | Mike Bostock; Cynthia Brewer, Mark Harrower and The Pennsylvania State University (ColorBrewer color schemes, Apache-2.0) |
| d3-selection | 3.0.0 | Mike Bostock |
| d3-shape | 3.2.0 | Mike Bostock |
| d3-time | 3.1.0 | Mike Bostock |
| d3-time-format | 4.1.0 | Mike Bostock |
| d3-timer | 3.0.1 | Mike Bostock |
| d3-transition | 3.0.1 | Mike Bostock |
| d3-zoom | 3.0.0 | Mike Bostock |
| internmap | 2.0.3 | Mike Bostock |

#### BSD-3-Clause

| Package | Version | Copyright holders |
| --- | --- | --- |
| d3-array | 2.12.1 | Mike Bostock |
| d3-ease | 3.0.1 | Mike Bostock; Robert Penner |
| d3-path | 1.0.9 | Mike Bostock |
| d3-sankey | 0.12.3 | Mike Bostock |
| d3-shape | 1.3.7 | Mike Bostock |

#### Apache-2.0 OR MPL-2.0

| Package | Version | Copyright holders |
| --- | --- | --- |
| dompurify | 3.4.12 | Cure53 and other contributors |

## License texts

### MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The copyright notice for each MIT-licensed component is the holder named for it above.

### ISC License

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted,
provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF
THIS SOFTWARE.

The copyright notice for each ISC-licensed component is the holder named for it above.

### BSD 3-Clause License

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
   disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
   disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products
   derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The copyright notice for each BSD-licensed component is the holder named for it above (for highlight.js, in the list of
bundled libraries).

### Apache License 2.0

The standard text, https://www.apache.org/licenses/LICENSE-2.0.

### SIL Open Font License 1.1

See `modules/web/static/fonts/OFL.txt` (also next to the Clean fonts, `modules/web/static/clean/f/`).

## Not in this repository

Programs that you install yourself and the hub only runs are not bundled and have their own licenses: Claude Code,
tmux, git, Tailscale and the browsers. The release packages built with `tools/package` download their runtimes at build
time (python-build-standalone, Node.js, git, GitHub CLI, ffmpeg, chrome-devtools-mcp; versions, licenses and sources in
`tools/package/versions.json`); they are not stored in the repository, and their license files go into the package
(`licenses/`). Chrome for Testing is not in the packages: it is downloaded separately from its own address.
