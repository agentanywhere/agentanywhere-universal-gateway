.PHONY: help build up down logs restart clean test lint format

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build Docker images
	docker compose build

up: ## Start all services
	docker compose up -d

down: ## Stop all services
	docker compose down

logs: ## View gateway logs
	docker compose logs -f gateway

restart: ## Restart gateway service
	docker compose restart gateway

clean: ## Remove containers and volumes
	docker compose down -v
	docker system prune -f

test: ## Run all tests
	pytest

test-unit: ## Run unit tests only
	pytest tests/ -m "not integration"

test-integration: ## Run integration tests only
	pytest tests/ -m integration

test-cov: ## Run tests with coverage
	pytest --cov=app --cov=admin --cov-report=html --cov-report=term

test-watch: ## Run tests in watch mode
	pytest-watch

lint: ## Run the same lint as CI (needs: pip install ruff)
	python -m compileall -q app admin
	ruff check app admin tests --select F821,F823

format: ## Format code (needs: pip install black isort; CI does not check formatting)
	black app/
	isort app/

dev: ## Start in development mode
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

install: ## Install dependencies
	pip install -r requirements.txt

monitoring: ## Start with monitoring (Prometheus + Grafana)
	docker compose --profile monitoring up -d

health: ## Check gateway health
	curl http://localhost:8000/health

metrics: ## View metrics
	curl http://localhost:8000/metrics

