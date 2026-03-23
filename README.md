## Database Migrations

Use Alembic for all schema initialization and upgrades.

```bash
alembic upgrade head
```

Do not use `Base.metadata.create_all()` in runtime or deployment paths.
