# LocalEC2 backend

The FastAPI backend runs directly on Ubuntu, uses MongoDB Atlas through the asynchronous PyMongo API, and controls local VMs through the `multipass` command.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m uvicorn app.main:app --reload
```

Configure `MONGODB_URI` and `MONGODB_DATABASE` in `.env`. No database migration, PostgreSQL service, or Docker container is required.
