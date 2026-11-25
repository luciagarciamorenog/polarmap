# Example output

`ES_2026-11-24.json` is a trimmed copy of one real run (Spain, 24-hour window to
24 Nov 2026), so the output can be understood without running anything.

| Field | Meaning |
|---|---|
| `headlines_read` | Spanish headlines in the 24-hour window, after cleaning |
| `share_in_no_debate` | Share of headlines the model assigned to no debate (most news is not a fight) |
| `side_a`, `side_b` | The two opposing positions the model defined for the debate (Spanish original + English) |
| `headlines_scored` | Headlines assigned to the debate and scored |
| `enough_data` | `false` if the debate has too few headlines or outlets: the numbers are indicative only |
| `ideological_polarization` | Esteban-Ray index on stance (0 = no camps, higher = separated camps) |
| `affective_polarization` | Hostility concentrated at the extremes and aimed at the other side |
| `hostile_share` | Share of the debate's headlines with attacking or belittling language |
| `outlet_effect` | How much of a headline's stance is explained by its outlet (η²), what chance alone gives (stances shuffled), and the permutation p-value |
| `outlets_sample.mean_stance` | Mean stance of the outlet's headlines, from −1 (side A) to +1 (side B), with a 90% bootstrap interval |
| `example_headlines` | One headline per side, original Spanish, with link |
