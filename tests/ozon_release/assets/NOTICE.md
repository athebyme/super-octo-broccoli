# Fixed browser test assets

These are public shell dependencies captured for the September 2026 acceptance
checks, reused only by the synthetic offline browser harness. `manifest.json`
records the exact requested URL, content type and SHA-256. It contains no seller
photos, database snapshots or credentials. Unknown external URLs fail the check.

- Tailwind browser bundle: MIT, `TAILWIND-LICENSE.txt`. Bundled dependency notices
  are retained in `tailwind.js`.
- Alpine core (observed version 3.17.4), collapse and focus: MIT,
  `ALPINE-LICENSE.txt`. Third-party notices remain in the bundles.
- Inter and Instrument Serif: SIL OFL 1.1, `INTER-OFL.txt` and
  `INSTRUMENT-SERIF-OFL.txt`; fonts are unmodified. `fonts.css` preserves the
  font-family, weight and Unicode subsets requested by the application shell.

The browser maps the template's CDN URLs to these exact bytes. This proves UI
behavior against the fixed dependencies; it does not prove current CDN
availability or equality to a later response from an unversioned URL. Production
asset delivery remains a separate release check. Update the bundle explicitly,
with source/hash/license review, when shell dependency behavior changes.
