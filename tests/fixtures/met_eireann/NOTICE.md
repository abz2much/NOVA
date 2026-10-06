# Met Éireann example warnings

These files are Met Éireann's own published examples, unchanged:

- `json/` from https://opendata2.met.ie/opendata2/warningsAnnouncement/JSONExample.zip
- `cap/` from https://opendata2.met.ie/opendata2/warningsAnnouncement/CAPExample.zip
  (`20260908071755.xml` is the RSS index of the other files)

Source: Met Éireann. Open data under Creative Commons Attribution 4.0; the
headline and description of a warning must not be altered. They are used
here as test data for Nova's Met Éireann warnings source.

Note: every CAP example has `<status>Test</status>`. Tests that need an
actual warning make a copy in memory with only the status changed.
