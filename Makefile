PYTHON ?= python3
DOCKER ?= sudo docker

.PHONY: install preflight plan run api nm-build nm-check test

install:
	$(PYTHON) -m pip install -r requirements.txt

preflight:
	PYTHONPATH=src $(PYTHON) -m stillemap.cli preflight

plan:
	PYTHONPATH=src $(PYTHON) -m stillemap.cli run --address "10 Downing Street, London" --no-run

run:
	PYTHONPATH=src $(PYTHON) -m stillemap.cli run --address "10 Downing Street, London"

api:
	PYTHONPATH=src uvicorn stillemap.api:app --reload --port 8080

nm-build:
	$(DOCKER) build -t stillemap/noisemodelling:6.0.0 -f docker/noisemodelling/Dockerfile docker/noisemodelling

nm-check:
	$(DOCKER) run --rm stillemap/noisemodelling:6.0.0 /opt/noisemodelling/bin/ScriptRunner --help

test:
	PYTHONPATH=src pytest -q
