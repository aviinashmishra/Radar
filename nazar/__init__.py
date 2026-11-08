"""Nazar: one eye on every market.

Nazar runs one full competitor radar per sector (the `radar` package, loaded once per sector under its own name with its
own config folder, database and schedule) inside a single process, and puts a hub on top that reads all of them together:
cross-sector pulse, market tape, company directory, themes, India-vs-world lens, and a switcher into each sector's own
dashboard.

    python -m nazar            -> http://127.0.0.1:8080
"""
__version__ = "1.0"
