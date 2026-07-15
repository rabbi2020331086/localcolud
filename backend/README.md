# LocalEC2 backend

The FastAPI service is the control plane. It stores desired state in MongoDB and dispatches durable,
leased jobs to approved Ubuntu compute-node agents. It never launches a VM directly on the machine
running the API.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m uvicorn app.main:app --reload
```

Configure `MONGODB_URI`, secrets, and allowed frontend origins in `.env`. A compute machine must be
enrolled from the separate admin console, run the generated installer, report Vagrant/libvirt/KVM
capabilities, and be approved before the scheduler can reserve its resources.
