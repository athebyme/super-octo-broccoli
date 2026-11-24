# Pinned browser runtimes

The application shell loads its Tailwind and Alpine dependencies from this
directory on the same origin. The four shell JavaScript files are unmodified
copies of the corresponding hash-verified assets in
`tests/ozon_release/assets/manifest.json`.
The shell's dependency filenames carry the resolved versions so a version
change is an explicit template update.

| Asset | Resolved version | SHA-256 | License |
| --- | --- | --- | --- |
| `tailwindcss-3.4.17-play-cdn.js` | Tailwind CSS 3.4.17 browser bundle | `176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15` | MIT, `TAILWIND-LICENSE.txt` |
| `alpine-3.17.4.global.min.js` | Alpine core 3.17.4 | `232519394c6c8fdba6f362b1d9da16106db513cdbf899011f00daab4051df31c` | MIT, `ALPINE-LICENSE.txt` |
| `alpine-collapse-3.17.4.min.js` | Alpine Collapse 3.17.4 | `c7661d4e2cf0465e3cd693190debb5f592ac72dcc4cfe650581273767558b27b` | MIT, `ALPINE-LICENSE.txt` |
| `alpine-focus-3.17.4.min.js` | Alpine Focus 3.17.4 | `ea7e215444f5110619549621cd0760cedfe273f708b144d4e658a87b702555f9` | MIT, `ALPINE-LICENSE.txt` |

`templates/base.html` loads Tailwind before its guarded utility configuration.
It loads Collapse and Focus before `sh-ui.js` and Alpine core, preserving the
Alpine plugin and `alpine:init` order. The login page loads the same Alpine core.
The font stylesheet remains an optional CDN request with system-font fallbacks.

The four JavaScript assets add 490,669 bytes and the two license copies add
2,163 bytes before HTTP compression. The bundles retain their upstream notices.

`vue-3.4.38.global.prod.js` is Vue 3.4.38's production global build, previously
loaded from jsDelivr by the catalogue. The bytes are unmodified. SHA-256:
`b50eeefe35d41636bb96c92b40f1df0b4fb7914e07b3c625b1ec15e9748767b9`.
License: MIT, `vue-LICENSE.txt`.

`vue-3.5.46.global.prod.js` is copied unmodified from `dist/vue.global.prod.js`
in the official npm package tarball `https://registry.npmjs.org/vue/-/vue-3.5.46.tgz`.
The package is pinned in `frontend/package-lock.json`. Tarball SHA-256:
`2f8d8d22497e8151701e18ed3fe6a8134b9924f3b9601dd4783a82618caaf8d6`.
Runtime SHA-256: `c2e4b888ecbb12c7b24fd5e8bf65483ceaab73c37b79402f7968023977ed3e81`.
License: MIT, `vue-3.5.46-LICENSE.txt`.
