"""Red team interop: other tools' red team files and probes brought to Martian.

- :mod:`martian.redteam.interop.promptfoo`: a promptfoo red team config as a version 2 run file, and
  its results as red team runs in the local store (``martian redteam convert promptfoo`` and
  ``martian redteam import promptfoo``). promptfoo is not needed.
- :mod:`martian.redteam.interop.garak`: garak's probes sent to a run file's target through garak's
  ``function`` generator, and garak's report as a red team run (``martian redteam garak`` and
  ``martian redteam import garak``). garak is not bundled.

``docs/redteam-interop.md`` documents both.
"""
