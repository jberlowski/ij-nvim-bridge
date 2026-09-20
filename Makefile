.DEFAULT_GOAL := help
SHELL := /bin/bash

IMAGE   ?= ij-nvim-harness:base
VENV    ?= .venv
PY      := $(VENV)/bin/python

.PHONY: help image canary venv test test-fast harness shell watch clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

image: ## Build the harness image (IntelliJ, Neovim, LazyVim, fixture)
	docker build -f harness/Dockerfile -t $(IMAGE) .

canary: ## Build the canary plugin against the pinned IDE
	./scripts/build-canary.sh

venv: $(VENV)/bin/pytest ## Create the test virtualenv
$(VENV)/bin/pytest:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r tests/requirements.txt

test: venv ## Run the full sufficiency suite (starts IntelliJ; slow)
	cd tests && ../$(PY) -m pytest

test-fast: venv ## Only the tests that do not start IntelliJ
	cd tests && ../$(PY) -m pytest -k "TestEnvironment or TestFixture"

harness: ## Start a container and leave it up, with IntelliJ on the fixture
	@name=$$(docker run -d -p 6080:6080 --memory 6g --shm-size 512m $(IMAGE) sleep infinity); \
	echo "container: $$name"; \
	sleep 3; \
	docker exec -u dev $$name bash -lc 'mkdir -p ~/.config/JetBrains/IntelliJIdea2026.2/options'; \
	docker cp canary/build/distributions/canary-0.1.0.zip $$name:/tmp/plugin.zip 2>/dev/null \
	  && docker exec -u dev $$name bash -lc 'mkdir -p ~/.local/share/JetBrains/IntelliJIdea2026.2 && cd ~/.local/share/JetBrains/IntelliJIdea2026.2 && unzip -oq /tmp/plugin.zip' \
	  || echo "(no canary built; run make canary)"; \
	docker exec -u dev -d $$name bash -lc 'DISPLAY=:99 /opt/idea/bin/idea /work/fixture'; \
	echo "watch it: http://localhost:6080/vnc.html"; \
	echo "stop it:  docker rm -f $$name"

shell: ## Interactive shell in a throwaway container
	docker run --rm -it -p 6080:6080 -u dev --entrypoint bash $(IMAGE) -l

watch: ## Open the live noVNC view
	open http://localhost:6080/vnc.html

clean: ## Remove stopped harness containers and test artifacts
	-docker ps -aq --filter 'name=ij-nvim-' | xargs -r docker rm -f
	rm -rf tests/artifacts
