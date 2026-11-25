# Social layer (experimental)

_Not part of the public interface yet. Moved here from the main README on 25 Nov 2026._


`pipeline_social.py` runs after a press run and reuses its debates, poles and 24-hour window, so press and social media are measured with the same ruler:

- **Official political communication:** press releases from the Government (La Moncloa) and the parties, read from their RSS feeds (found automatically on each site). Government and parties are treated as senders, like outlets: how hostile their language is, how often they frame "us vs. them", where they stand on each debate and which debates they push compared with the press. Government and opposition are measured together so the layer is not aimed at one side. Source for La Moncloa: "Origen de los datos: sitio web de lamoncloa.gob.es. Ministerio de la Presidencia."
- **Bluesky accounts:** what parties, politicians, journalists, outlets and government institutions post (public API, no key).
- **Bluesky replies:** how the public answers them. Authors are pseudonymised (salted hash) and only used in aggregate.
- **YouTube comments** on videos from outlets and parties (free YouTube Data API key).

The report compares, per debate and layer: the polarization index, mean |stance|, share at the centre, hostility, "us vs. them" framing and the government ↔ opposition axis, plus how each layer leans. Bluesky's Spanish user base leans left, so the report also shows which groups of listed accounts actually posted; a layer missing one side is reported as such, not compared as if it were balanced.

```bash
python3 pipeline_social.py --check                         # verify the account and channel list (free)
python3 pipeline_social.py --press-run snapshot_ES_<date>  # ~1.5-2.5 $ per run
./semanal.sh                                               # weekly: press + social
```

