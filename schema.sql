DROP TABLE IF EXISTS votes;
DROP TABLE IF EXISTS voters;

CREATE TABLE voters (
  national_id      TEXT PRIMARY KEY,
  full_name        TEXT NOT NULL,
  eligible         INTEGER NOT NULL DEFAULT 1,
  has_voted        INTEGER NOT NULL DEFAULT 0,
  template_method  TEXT,
  template_blob    BLOB,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE votes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  national_id TEXT NOT NULL,
  choice      TEXT NOT NULL,
  cast_at     TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(national_id) REFERENCES voters(national_id)
);
