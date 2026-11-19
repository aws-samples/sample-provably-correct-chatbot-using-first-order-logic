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
`cdk/package-lock.json`, and the image also installs the cvc5 and kiro-cli
binaries (see `cdk/docker/Dockerfile`). None of them is redistributed in this
repository.
