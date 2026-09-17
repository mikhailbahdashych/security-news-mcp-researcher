"""The knowledge base: durable capture, chunking and retrieval over the app's data.

The layout mirrors the responsibilities rather than the tables:

``models``      the ORM tables (created by ``create_all`` like every other table)
``schema``     the two virtual tables' frozen DDL, their versions and the rebuilds
``chunking``   pure Markdown chunking
``fts``        the FTS5 query builder — the app's *second* escaping rule
``entities``   the CVE regex and friends
``embeddings`` the ``Embedder`` protocol and the no-op embedder
``store``      ``KnowledgeStore`` and its SQLite implementation
``retrieval``  fusion, collapse and ``hybrid_search``
``urls``       canonicalising a URL so the same article is the same entry
``capture``    writing: capture, snapshots, refresh, soft delete, merge
``service``    ``KbService``, the one door the API and the chat tools use
"""
