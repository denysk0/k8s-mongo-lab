SHELL := /bin/bash

CLUSTER_NAME := mongo-lab
NAMESPACE := mongo
PORT := 5050
APP_DIR := app
VENV := $(APP_DIR)/.venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
APP_PID_FILE := .app.pid
APP_LOG_FILE := .app.log

.PHONY: help venv deps cluster-up manifests-up init-job wait-mongo wait-init wait-primary rs-status seed bootstrap app app-bg stop-app status ports cluster-down down reset

help:
	@printf "Available targets:\n"
	@printf "  make up         - create kind cluster, apply manifests, seed DB, start Flask in background\n"
	@printf "  make bootstrap  - create kind cluster, apply manifests, wait for RS, install deps, seed DB\n"
	@printf "  make app        - run Flask in foreground on port $(PORT)\n"
	@printf "  make app-bg     - run Flask in background on port $(PORT)\n"
	@printf "  make stop-app   - stop Flask on port $(PORT), including manual runs\n"
	@printf "  make status     - show nodes, pods, replica set and API status\n"
	@printf "  make seed       - reseed demo accounts\n"
	@printf "  make down       - stop Flask and delete kind cluster\n"
	@printf "  make reset      - recreate everything from scratch\n"

venv:
	@if [ ! -x "$(PYTHON)" ]; then \
		python3 -m venv "$(VENV)"; \
	fi

deps: venv
	@"$(PIP)" install -r "$(APP_DIR)/requirements.txt"

cluster-up:
	@if kind get clusters | grep -qx '$(CLUSTER_NAME)'; then \
		echo "kind cluster '$(CLUSTER_NAME)' already exists"; \
	else \
		kind create cluster --config kind-config.yaml; \
	fi

manifests-up:
	kubectl apply -f manifests/01-namespace.yaml
	kubectl apply -f manifests/02-headless-svc.yaml
	kubectl apply -f manifests/03-statefulset.yaml
	kubectl apply -f manifests/05-per-pod-nodeport.yaml

init-job:
	-kubectl -n "$(NAMESPACE)" delete job mongo-init --ignore-not-found
	kubectl apply -f manifests/04-init-replicaset.yaml

wait-mongo:
	kubectl -n "$(NAMESPACE)" rollout status statefulset/mongo --timeout=180s
	kubectl -n "$(NAMESPACE)" wait --for=jsonpath='{.status.readyReplicas}'=3 statefulset/mongo --timeout=180s

wait-init:
	kubectl -n "$(NAMESPACE)" wait --for=condition=complete job/mongo-init --timeout=180s

wait-primary:
	@for i in $$(seq 1 60); do \
		primary="$$(kubectl -n "$(NAMESPACE)" exec mongo-0 -- mongosh --quiet --eval 'try { const h = db.hello(); h.isWritablePrimary ? (h.me || "") : (h.primary || "") } catch(e) { "" }' 2>/dev/null | tail -n 1)"; \
		if [ -n "$$primary" ]; then \
			echo "Replica set PRIMARY is $$primary"; \
			exit 0; \
		fi; \
		echo "Waiting for replica set PRIMARY... ($$i/60)"; \
		sleep 2; \
	done; \
	echo "Timed out waiting for replica set PRIMARY" >&2; \
	exit 1

rs-status:
	kubectl -n "$(NAMESPACE)" exec mongo-0 -- mongosh --quiet --eval \
		'rs.status().members.forEach(m => print(m.name.split(".")[0].padEnd(10), m.stateStr))'

seed: deps
	cd "$(APP_DIR)" && .venv/bin/python seed.py

bootstrap: cluster-up manifests-up wait-mongo init-job wait-init wait-primary deps seed rs-status

app: deps
	cd "$(APP_DIR)" && PORT="$(PORT)" .venv/bin/python server.py

app-bg: deps
	@if [ -f "$(APP_PID_FILE)" ] && kill -0 "$$(cat "$(APP_PID_FILE)")" 2>/dev/null; then \
		echo "Flask app already running with PID $$(cat "$(APP_PID_FILE)")"; \
	else \
		cd "$(APP_DIR)" && PORT="$(PORT)" nohup .venv/bin/python server.py > "../$(APP_LOG_FILE)" 2>&1 & echo $$! > "../$(APP_PID_FILE)"; \
		echo "Flask app started on http://localhost:$(PORT) (PID $$(cat "$(APP_PID_FILE)"))"; \
		echo "Log file: $(APP_LOG_FILE)"; \
	fi

stop-app:
	@stopped=0; \
	if [ -f "$(APP_PID_FILE)" ]; then \
		pid="$$(cat "$(APP_PID_FILE)")"; \
		if kill -0 "$$pid" 2>/dev/null; then \
			kill "$$pid"; \
			echo "Stopped Flask app PID $$pid from $(APP_PID_FILE)"; \
			stopped=1; \
		else \
			echo "PID $$pid from $(APP_PID_FILE) is not running"; \
		fi; \
		rm -f "$(APP_PID_FILE)"; \
	fi; \
	pids="$$(lsof -ti:$(PORT) 2>/dev/null || true)"; \
	if [ -n "$$pids" ]; then \
		echo "$$pids" | xargs kill; \
		echo "Stopped process(es) listening on port $(PORT): $$pids"; \
		stopped=1; \
	fi; \
	if [ "$$stopped" -eq 0 ]; then \
		echo "No Flask process found on port $(PORT)"; \
	fi

status:
	@printf "\n== kind clusters ==\n"
	@kind get clusters || true
	@printf "\n== kubernetes nodes ==\n"
	@kubectl get nodes
	@printf "\n== mongo pods ==\n"
	@kubectl -n "$(NAMESPACE)" get pods -o wide
	@printf "\n== replica set ==\n"
	@kubectl -n "$(NAMESPACE)" exec mongo-0 -- mongosh --quiet --eval \
		'rs.status().members.forEach(m => print(m.name.split(".")[0].padEnd(10), m.stateStr))'
	@printf "\n== nodeports ==\n"
	@$(MAKE) --no-print-directory ports
	@printf "\n== api status ==\n"
	@curl -s "http://localhost:$(PORT)/api/status" | python3 -m json.tool

ports:
	@for p in 30017 30018 30019; do \
		nc -z -w 2 localhost $$p && echo "$$p OK" || echo "$$p FAIL"; \
	done

cluster-down:
	@if kind get clusters | grep -qx '$(CLUSTER_NAME)'; then \
		kind delete cluster --name "$(CLUSTER_NAME)"; \
	else \
		echo "kind cluster '$(CLUSTER_NAME)' does not exist"; \
	fi

down: stop-app cluster-down

up: bootstrap app-bg status

reset: down up
