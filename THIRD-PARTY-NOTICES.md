# Third-party notices

This sample is licensed under MIT-0 (see [LICENSE](LICENSE)). It also includes
the following third-party material, which is not covered by that license.

## Synthea sample patient data

- **File:** `sentences/synthea_sample_data_fhir_dstu2_latest.zip` (109 patients,
  HL7 FHIR DSTU2 bundles)
- **Source:** the Synthea sample data downloads,
  <https://synthetichealth.github.io/downloads.html>
- **Produced by:** Synthea™, the synthetic patient generator by The MITRE
  Corporation (<https://github.com/synthetichealth/synthea>)

The records are synthetic. They describe simulated patients, not real people,
and contain no real patient data. The Synthea project publishes this sample data
for use without restriction and appreciates a citation:

> Jason Walonoski, Mark Kramer, Joseph Nichols, Andre Quina, Chris Moesel,
> Dylan Hall, Carlton Duffett, Kudakwashe Dube, Thomas Gallagher, Scott
> McLachlan. Synthea: An approach, method, and software mechanism for generating
> synthetic patients and the synthetic electronic health care record. Journal of
> the American Medical Informatics Association, Volume 25, Issue 3, March 2018,
> Pages 230–238. <https://doi.org/10.1093/jamia/ocx079>

The Docker image unpacks the zip so the web app's "Add theory" form can offer
the patients as sample sources.

## Dependencies

Python, Node.js and container dependencies are installed at build time from
their own registries under their own licenses; they are listed in
`uv.lock`, `cdk/docker/requirements.lock`, `frontend/package-lock.json` and
`cdk/package-lock.json`. All of them are under permissive licenses (MIT,
Apache-2.0, BSD, ISC, PSF, MPL-2.0, BlueOak-1.0.0, 0BSD, Zlib, CC0-1.0; the
`case` npm package is dual MIT/GPL-3.0 and is used under MIT). None of them
is redistributed in this repository.

The container image (`cdk/docker/Dockerfile`) also downloads, at build time:

- **cvc5** 1.3.1, the static Linux arm64 release
  (<https://github.com/cvc5/cvc5>), under the modified BSD license. Its
  default build, which the release uses, links GMP and MPFR (LGPL-3.0) and
  no GPL-only libraries.
- **kiro-cli**, from <https://cli.kiro.dev/install>, under its own terms
  (<https://kiro.dev>). Theory builds need a Kiro account and API key.
- **NLTK data** (`punkt`, `punkt_tab`), under the NLTK data's own terms
  (<https://www.nltk.org/nltk_data/>).

At runtime the app downloads the Wikipedia pages a user asks for. Wikipedia
text is licensed CC BY-SA 4.0 (<https://en.wikipedia.org/wiki/Wikipedia:Copyrights>);
none is redistributed in this repository.
