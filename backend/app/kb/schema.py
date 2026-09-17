"""The two virtual tables, their triggers, and the DDL that is frozen once merged.

``create_all`` knows nothing about virtual tables, so these are explicit DDL run by
``init_db``. Both statements are **frozen**:

* ``kb_chunk_vec`` is a ``vec0`` table and vec0 has no ``ALTER``. Adding a metadata
  column later means dropping the table and re-inserting every vector, so the six
  metadata columns below are the complete filter vocabulary for KNN, forever
  (spec S1). Only ``= != < >`` work on them, there is a hard limit of 16, and an
  auxiliary (``+``) column cannot appear in a KNN ``WHERE`` — so nothing that is
  not derivable from the row is ever stored here.
* ``kb_chunks_fts``'s tokenizer is baked into its ``CREATE``. ``tokenchars '-'``
  is deliberately not set (it looks attractive for CVE ids and would break every
  partial match) and there is no ``porter`` stemmer (it would mangle ``log4j``,
  ``xz``, vendor names and hashes) — spec S2.

A change to either is a **versioned rebuild**, never an edit in place: bump the
matching ``*_DDL_VERSION``, and the app reports "index format outdated" until the
user presses the rebuild button. Nothing here ever rebuilds by itself.
"""

from __future__ import annotations

#: Float32 elements per embedding. Baked into :data:`VEC_DDL`; the stored
#: ``kb_schema_version`` must agree with it or the index is "outdated".
VEC_DIMENSIONS = 1024

#: The FTS5 tokenizer, repeated here so callers can report it without parsing DDL.
FTS_TOKENIZER = "unicode61 remove_diacritics 2"

#: **Frozen.** See the module docstring before touching a character of this.
VEC_DDL = f"""CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunk_vec USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    entry_id INTEGER,
    entry_kind TEXT,
    chunk_kind TEXT,
    reviewed INTEGER,
    authorship TEXT,
    published_day INTEGER,
    embedding float[{VEC_DIMENSIONS}]
)"""

#: **Frozen.** An external-content FTS5 index over ``kb_chunks.text``.
FTS_DDL = f"""CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
    text,
    content='kb_chunks',
    content_rowid='id',
    tokenize="{FTS_TOKENIZER}"
)"""

#: Keeping an external-content FTS5 index in step is the caller's job, so the three
#: content-sync triggers are part of the schema rather than of any one code path.
FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS kb_chunks_ai AFTER INSERT ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(rowid, text) VALUES (new.id, new.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS kb_chunks_ad AFTER DELETE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS kb_chunks_au AFTER UPDATE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO kb_chunks_fts(rowid, text) VALUES (new.id, new.text);
END""",
)

#: A virtual table cannot be the child of a foreign key, so ``PRAGMA
#: foreign_keys=ON`` does nothing for ``kb_chunk_vec``. This trigger is the only
#: thing that keeps it in step — and because an FK cascade *does* fire the child
#: table's triggers, a cascaded chunk delete cleans the vector as well.
VEC_TRIGGER = """CREATE TRIGGER IF NOT EXISTS kb_chunks_ad_vec AFTER DELETE ON kb_chunks BEGIN
    DELETE FROM kb_chunk_vec WHERE chunk_id = old.id;
END"""


def virtual_table_statements() -> tuple[str, ...]:
    """Every statement ``init_db`` runs after ``create_all``, in order.

    The triggers reference both ``kb_chunks`` (an ordinary table ``create_all``
    has just made) and the virtual tables, so the tables come first.
    """
    return (FTS_DDL, VEC_DDL, *FTS_TRIGGERS, VEC_TRIGGER)


__all__ = [
    "FTS_DDL",
    "FTS_TOKENIZER",
    "FTS_TRIGGERS",
    "VEC_DDL",
    "VEC_DIMENSIONS",
    "VEC_TRIGGER",
    "virtual_table_statements",
]
