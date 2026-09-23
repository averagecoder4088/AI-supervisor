"""Declarative base for all SQLAlchemy ORM models.

Kept separate from models.py so Alembic's env.py can import the shared
metadata without importing the full model module (and its dependencies)
more than once.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base class for Order Supervisor ORM models."""
