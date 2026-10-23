# data/ (git-ignored)

- `raw/<session_id>/` – recordings, written once by `sglab record`. Never edit by hand.
- `raw/_incoming/` – sessions currently recording (or interrupted → `sglab recover`).
- `processed/<session_id>/<stream>/` – Layer 1–2 outputs; delete and rerun any time.
- `datasets/` – training tables built from many sessions (Layer 3).
- `catalog.sqlite` – index; `sglab reindex` rebuilds it from `raw/*/session.json`.

Back up `raw/` after every collection day.
