.PHONY: backend-install backend-dev frontend-install frontend-dev admin-install admin-dev test lint
backend-install:
	cd backend && python -m pip install -e ".[dev]"
backend-dev:
	cd backend && python -m uvicorn app.main:app --reload
frontend-install:
	cd frontend && pnpm install
frontend-dev:
	cd frontend && pnpm run dev
admin-install:
	cd admin-frontend && pnpm install
admin-dev:
	cd admin-frontend && pnpm run dev
test:
	cd backend && python -m pytest && cd ../frontend && pnpm test -- --run
lint:
	cd backend && python -m ruff check . && python -m mypy app && cd ../frontend && pnpm run lint && pnpm run typecheck && cd ../admin-frontend && pnpm run lint && pnpm run typecheck
