"""Research-vessel sampling importer and review service.

The package intentionally depends only on the Python standard library for its
embedded (SQLite) mode.  Production can opt into PostgreSQL by installing
``psycopg`` and setting ``DATABASE_URL``.
"""

__version__ = "1.0.0"
