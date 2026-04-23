# =============================================================================
# VICINITY — Makefile
# =============================================================================
#
#   make build          build all images (API + Airflow + Frontend)
#   make build-api      build API image only
#   make build-af       build Airflow image only
#   make build-fe       build Frontend image only
#
#   make up             start local stack (Redis + API + Frontend)
#   make down           stop local stack
#   make logs           tail local logs
#
#   make test           run unit tests
#   make lint           ruff check
#   make ci             lint → test → build
#
#   make all            build → push → upload → deploy → deploy-af → status
#   make redeploy       rebuild API → push → deploy (fast)
#   make redeploy-fe    rebuild Frontend → push → deploy-fe (fast)
#
#   make deploy         deploy API + Redis + Frontend + MCP on VM
#   make deploy-fe      redeploy frontend only (no API restart)
#   make deploy-af      deploy Airflow on VM
#
#   make push           push all images to Artifact Registry
#   make upload         bundle project + SCP to VM
#   make status         check production health
#   make ssh            SSH into VM
#   make expose-af      open Airflow UI (port 8081)
#   make hide-af        close Airflow UI (port 8081)
#   make clean          remove temp files + stop local stack
#
# =============================================================================

# ── Shell ───────────────────────────────────────────────────
ifeq ($(OS),Windows_NT)
    SHELL := C:/Program Files/Git/bin/bash.exe
else
    SHELL := /bin/bash
endif

# ── Config ──────────────────────────────────────────────────
-include deploy.env

_check-config:
	@if [ -z "$(GCP_PROJECT)" ]; then \
		echo "ERROR: deploy.env not found or GCP_PROJECT not set."; \
		echo "Create deploy.env: GCP_PROJECT, GCP_REGION, AR_REPO, INSTANCE, ZONE, MACHINE, VM_TAG"; \
		exit 1; \
	fi
	@echo "Config: project=$(GCP_PROJECT) instance=$(INSTANCE) zone=$(ZONE)"

# ── Derived ─────────────────────────────────────────────────
AR_HOST        = $(GCP_REGION)-docker.pkg.dev
AR_PREFIX      = $(AR_HOST)/$(GCP_PROJECT)/$(AR_REPO)
IMG_API        = $(AR_PREFIX)/vicinity-api
IMG_AF         = $(AR_PREFIX)/vicinity-airflow
IMG_FE         = $(AR_PREFIX)/vicinity-frontend
LOCAL_API      = vicinity-api
LOCAL_AF       = vicinity-airflow
LOCAL_FE       = vicinity-frontend
TIMESTAMP     := $(shell bash -c 'date +%Y%m%d-%H%M%S')
BUNDLE         = vicinity-project.tar.gz
STATIC_IP_NAME = vicinity-ip-$(INSTANCE)

.PHONY: test lint ci \
        build build-api build-af build-fe \
        up down logs health \
        auth push push-api push-af push-fe \
        bundle upload \
        deploy deploy-fe deploy-af \
        _ensure-vm _ensure-firewall _ensure-ar _wait-ssh _promote-ip \
        ssh status \
        all redeploy redeploy-fe clean \
        expose-af hide-af \
        _check-config check check-vm


# =============================================================================
# TEST / LINT
# =============================================================================

lint:
	@echo "====== Lint ======"
	python -m ruff check app/ mcp_vicinity/ scripts/ tests/ --fix

test:
	@echo "====== Tests ======"
	python -m pytest tests/unit/ -v --tb=short -q

ci: lint test build
	@echo "====== CI passed ======"


# =============================================================================
# BUILD
# =============================================================================

build: build-api build-af build-fe
	@echo ""
	@echo "====== Built images ======"
	@docker images --filter "reference=vicinity-*" \
		--format "  {{.Repository}}:{{.Tag}}  {{.Size}}"

build-api:
	@echo "====== Building API ======"
	docker build -f docker/Dockerfile.api -t $(LOCAL_API):latest .

build-af:
	@echo "====== Building Airflow ======"
	docker build -f airflow/Dockerfile -t $(LOCAL_AF):latest ./airflow

build-fe:
	@echo "====== Building Frontend ======"
	docker build -f docker/Dockerfile.frontend -t $(LOCAL_FE):latest .


# =============================================================================
# LOCAL DEV
# =============================================================================

up:
	@echo "====== Starting local stack ======"
	docker compose -f docker/docker-compose.yml up -d --build
	@echo ""
	@echo "Waiting for API..."
	@for i in $$(seq 1 30); do \
		if curl -sf http://localhost:8000/ping > /dev/null 2>&1; then \
			echo "  API ready ($${i}s)"; break; \
		fi; \
		if [ $$i -eq 30 ]; then echo "  API failed to start in 30s"; fi; \
		sleep 1; \
	done
	@echo "Waiting for Frontend..."
	@for i in $$(seq 1 20); do \
		if curl -sf http://localhost:3000 > /dev/null 2>&1; then \
			echo "  Frontend ready ($${i}s)"; break; \
		fi; \
		if [ $$i -eq 20 ]; then echo "  Frontend not ready (may still be starting)"; fi; \
		sleep 1; \
	done
	@echo ""
	@echo "====== Local Stack Ready ======"
	@echo "  Frontend  http://localhost:3000"
	@echo "  API       http://localhost:8000/docs"
	@echo "  MCP       http://localhost:8001/mcp"
	@echo "  Redis     localhost:6379"
	@echo ""

down:
	docker compose -f docker/docker-compose.yml down

logs:
	docker compose -f docker/docker-compose.yml logs -f --tail 50

health:
	@echo ""
	@echo "-- Local Health --"
	@curl -sf http://localhost:6379 > /dev/null 2>&1 \
		&& echo "  Redis     :6379   OK" \
		|| (redis-cli ping 2>/dev/null | grep -q PONG && echo "  Redis     :6379   OK" || echo "  Redis     :6379   FAIL")
	@curl -sf http://localhost:8000/ping > /dev/null 2>&1 \
		&& echo "  API       :8000   OK   http://localhost:8000/docs" \
		|| echo "  API       :8000   FAIL"
	@curl -sf http://localhost:3000 > /dev/null 2>&1 \
		&& echo "  Frontend  :3000   OK   http://localhost:3000" \
		|| echo "  Frontend  :3000   FAIL"
	@curl -sf http://localhost:8001/mcp > /dev/null 2>&1 \
		&& echo "  MCP       :8001   OK   http://localhost:8001/mcp" \
		|| echo "  MCP       :8001   FAIL"
	@echo ""


# =============================================================================
# GCP INFRASTRUCTURE (idempotent)
# =============================================================================

_promote-ip: _check-config
	@STATIC_EXISTS=$$(gcloud compute addresses describe $(STATIC_IP_NAME) \
		--project=$(GCP_PROJECT) --region=$(GCP_REGION) \
		--format="get(address)" 2>/dev/null || echo ""); \
	if [ -n "$$STATIC_EXISTS" ]; then \
		echo "Static IP $(STATIC_IP_NAME) — $$STATIC_EXISTS"; \
	else \
		CURRENT_IP=$$(gcloud compute instances describe $(INSTANCE) \
			--project=$(GCP_PROJECT) --zone=$(ZONE) \
			--format="get(networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null || echo ""); \
		if [ -z "$$CURRENT_IP" ]; then echo "ERROR: No external IP to promote."; exit 1; fi; \
		echo "Promoting $$CURRENT_IP → $(STATIC_IP_NAME)"; \
		gcloud compute addresses create $(STATIC_IP_NAME) \
			--project=$(GCP_PROJECT) --region=$(GCP_REGION) --addresses=$$CURRENT_IP; \
	fi

_ensure-vm: _check-config _ensure-firewall
	@VM_STATUS=$$(gcloud compute instances describe $(INSTANCE) \
		--project=$(GCP_PROJECT) --zone=$(ZONE) \
		--format="get(status)" 2>/dev/null || echo "NOT_FOUND"); \
	echo "VM $(INSTANCE): $$VM_STATUS"; \
	if [ "$$VM_STATUS" = "NOT_FOUND" ]; then \
		echo "Creating $(INSTANCE)..."; \
		gcloud compute instances create $(INSTANCE) \
			--project=$(GCP_PROJECT) --zone=$(ZONE) \
			--machine-type=$(MACHINE) \
			--boot-disk-size=60GB \
			--image-family=ubuntu-2204-lts \
			--image-project=ubuntu-os-cloud \
			--scopes=cloud-platform \
			--tags=$(VM_TAG) \
			--metadata=google-logging-enabled=true; \
	elif [ "$$VM_STATUS" = "TERMINATED" ] || [ "$$VM_STATUS" = "STOPPED" ]; then \
		echo "Starting $(INSTANCE)..."; \
		gcloud compute instances start $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE); \
		for i in $$(seq 1 30); do \
			S=$$(gcloud compute instances describe $(INSTANCE) \
				--project=$(GCP_PROJECT) --zone=$(ZONE) --format="get(status)" 2>/dev/null); \
			if [ "$$S" = "RUNNING" ]; then echo "  Running"; break; fi; sleep 2; \
		done; \
	fi
	@$(MAKE) _wait-ssh
	@$(MAKE) _promote-ip

_wait-ssh: _check-config
	@echo "Waiting for SSH..."
	@for i in $$(seq 1 60); do \
		if gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
			--command="echo ok" 2>/dev/null | grep -q ok; then \
			echo "  SSH ready"; exit 0; \
		fi; sleep 2; \
	done; echo "ERROR: SSH timeout after 120s"; exit 1

_ensure-firewall: _check-config
	@if gcloud compute firewall-rules describe vicinity-allow-api \
		--project=$(GCP_PROJECT) > /dev/null 2>&1; then \
		echo "Firewall — exists"; \
	else \
		echo "Creating firewall (80 + 8000 + 8001)..."; \
		gcloud compute firewall-rules create vicinity-allow-api \
			--project=$(GCP_PROJECT) \
			--allow=tcp:80,tcp:8000,tcp:8001 \
			--target-tags=$(VM_TAG) \
			--description="Vicinity API + Frontend"; \
	fi

_ensure-ar: _check-config
	@if gcloud artifacts repositories describe $(AR_REPO) \
		--project=$(GCP_PROJECT) --location=$(GCP_REGION) > /dev/null 2>&1; then \
		echo "AR repo $(AR_REPO) — exists"; \
	else \
		echo "Creating AR repo $(AR_REPO)..."; \
		gcloud artifacts repositories create $(AR_REPO) \
			--project=$(GCP_PROJECT) --repository-format=docker --location=$(GCP_REGION); \
	fi


# =============================================================================
# CHECK (read-only)
# =============================================================================

check: _check-config
	@echo ""
	@echo "====== $(GCP_PROJECT) / $(GCP_REGION) ======"
	@echo ""
	@echo "-- Images --"
	@gcloud artifacts docker images list $(AR_PREFIX) --project=$(GCP_PROJECT) \
		--format="table(package,version,updateTime)" --sort-by="~updateTime" 2>/dev/null || echo "  (none)"
	@echo ""
	@echo "-- Static IPs --"
	@gcloud compute addresses list --project=$(GCP_PROJECT) --filter="region:$(GCP_REGION)" \
		--format="table(name,address,status)" 2>/dev/null || echo "  (none)"

check-vm: _check-config
	@echo "-- VMs --"
	@gcloud compute instances list --project=$(GCP_PROJECT) \
		--format="table(name,zone,machineType,status,networkInterfaces[0].accessConfigs[0].natIP)"
	@echo ""
	@echo "-- Containers on $(INSTANCE) --"
	@gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
		--command="sudo docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'" \
		2>/dev/null || echo "  (cannot reach VM)"


# =============================================================================
# PUSH
# =============================================================================

auth: _check-config
	gcloud auth configure-docker $(AR_HOST) --quiet

push: auth _ensure-ar
	@echo "====== Pushing all to $(AR_PREFIX) ======"
	docker tag $(LOCAL_API):latest $(IMG_API):latest
	docker tag $(LOCAL_API):latest $(IMG_API):$(TIMESTAMP)
	docker tag $(LOCAL_AF):latest  $(IMG_AF):latest
	docker tag $(LOCAL_AF):latest  $(IMG_AF):$(TIMESTAMP)
	docker tag $(LOCAL_FE):latest  $(IMG_FE):latest
	docker tag $(LOCAL_FE):latest  $(IMG_FE):$(TIMESTAMP)
	docker push $(IMG_API):latest
	docker push $(IMG_API):$(TIMESTAMP)
	docker push $(IMG_AF):latest
	docker push $(IMG_AF):$(TIMESTAMP)
	docker push $(IMG_FE):latest
	docker push $(IMG_FE):$(TIMESTAMP)
	@echo "Pushed: latest + $(TIMESTAMP)"

push-api: auth _ensure-ar
	docker tag $(LOCAL_API):latest $(IMG_API):latest
	docker push $(IMG_API):latest

push-af: auth _ensure-ar
	docker tag $(LOCAL_AF):latest $(IMG_AF):latest
	docker push $(IMG_AF):latest

push-fe: auth _ensure-ar
	docker tag $(LOCAL_FE):latest $(IMG_FE):latest
	docker push $(IMG_FE):latest


# =============================================================================
# BUNDLE + UPLOAD
# =============================================================================

bundle:
	@echo "====== Bundling ======"
	tar czf $(BUNDLE) \
		--exclude="__pycache__" \
		--exclude=".git" \
		--exclude="*.pyc" \
		--exclude=".venv" \
		--exclude="node_modules" \
		--exclude="frontend/dist" \
		--exclude="airflow/logs/*" \
		--exclude="deploy.env" \
		app/ \
		mcp_vicinity/ \
		config/ \
		scripts/ \
		frontend/ \
		docker/docker-compose.yml \
		docker/Dockerfile.api \
		docker/Dockerfile.frontend \
		docker/nginx.conf \
		airflow/dags/ \
		airflow/docker-compose.yml \
		airflow/Dockerfile \
		airflow/requirements.txt \
		alembic/ \
		alembic.ini \
		requirements.txt \
		requirements-api.txt
	@ls -lh $(BUNDLE)

upload: _ensure-vm bundle
	@echo "====== Uploading to $(INSTANCE) ======"
	gcloud compute scp $(BUNDLE) $(INSTANCE):$(BUNDLE) --project=$(GCP_PROJECT) --zone=$(ZONE)
	gcloud compute scp .env $(INSTANCE):.env --project=$(GCP_PROJECT) --zone=$(ZONE)
	gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
		--command="mkdir -p vicinity-project && tar xzf $(BUNDLE) -C vicinity-project && rm $(BUNDLE) && cp .env vicinity-project/.env && echo 'Unpacked to ~/vicinity-project'"
	@rm -f $(BUNDLE)


# =============================================================================
# DEPLOY
# =============================================================================

deploy: _ensure-vm
	@echo "====== Deploying API + Redis + Frontend + MCP ======"
	$(eval VM_IP := $(shell gcloud compute instances describe $(INSTANCE) \
		--project=$(GCP_PROJECT) --zone=$(ZONE) \
		--format="get(networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null))
	@if [ -z "$(VM_IP)" ]; then echo "ERROR: No VM IP."; exit 1; fi
	@echo "  VM: $(VM_IP)"
	@echo '#!/bin/bash' > _deploy.sh
	@echo 'set -e' >> _deploy.sh
	@echo 'if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sudo sh; fi' >> _deploy.sh
	@echo 'gcloud auth print-access-token | sudo docker login -u oauth2accesstoken --password-stdin https://$(AR_HOST)' >> _deploy.sh
	@echo 'sudo docker pull $(IMG_API):latest' >> _deploy.sh
	@echo 'sudo docker pull $(IMG_FE):latest' >> _deploy.sh
	@echo 'sudo docker network create vicinity-net 2>/dev/null || true' >> _deploy.sh
	@echo 'sudo docker stop vicinity-mcp vicinity-frontend vicinity-api vicinity-redis 2>/dev/null || true' >> _deploy.sh
	@echo 'sudo docker rm   vicinity-mcp vicinity-frontend vicinity-api vicinity-redis 2>/dev/null || true' >> _deploy.sh
	@echo 'sudo docker run -d --name vicinity-redis --network vicinity-net --restart unless-stopped -v redis_data:/data redis:7-alpine redis-server --appendonly yes' >> _deploy.sh
	@echo 'sleep 3' >> _deploy.sh
	@echo 'sudo docker run -d --name vicinity-api --network vicinity-net --restart unless-stopped -p 8000:8000 --env-file $$HOME/.env -e REDIS_URL=redis://vicinity-redis:6379/0 -e PYTHONUNBUFFERED=1 -v $$HOME/vicinity-project/config:/app/config:ro $(IMG_API):latest' >> _deploy.sh
	@echo 'sleep 2' >> _deploy.sh
	@echo 'sudo docker run -d --name vicinity-frontend --network vicinity-net --restart unless-stopped -p 80:80 $(IMG_FE):latest' >> _deploy.sh
	@echo 'sudo docker run -d --name vicinity-mcp --network vicinity-net --restart unless-stopped -p 8001:8001 --env-file $$HOME/.env -e REDIS_URL=redis://vicinity-redis:6379/0 -e PYTHONUNBUFFERED=1 -v $$HOME/vicinity-project/config:/app/config:ro $(IMG_API):latest python -m mcp_vicinity --transport streamable-http --port 8001' >> _deploy.sh
	@echo 'echo ""' >> _deploy.sh
	@echo 'echo "=== DEPLOYED ==="' >> _deploy.sh
	@echo 'sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"' >> _deploy.sh
	gcloud compute scp _deploy.sh $(INSTANCE):_deploy.sh --project=$(GCP_PROJECT) --zone=$(ZONE)
	-gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
		--command="bash _deploy.sh && rm _deploy.sh"
	-@rm -f _deploy.sh 2>/dev/null
	@echo ""
	@echo "  Frontend  http://$(VM_IP)"
	@echo "  API       http://$(VM_IP):8000/docs"
	@echo "  MCP       http://$(VM_IP):8001/mcp"

deploy-fe: _ensure-vm
	@echo "====== Deploying Frontend only ======"
	@echo '#!/bin/bash' > _deploy_fe.sh
	@echo 'set -e' >> _deploy_fe.sh
	@echo 'gcloud auth print-access-token | sudo docker login -u oauth2accesstoken --password-stdin https://$(AR_HOST)' >> _deploy_fe.sh
	@echo 'sudo docker pull $(IMG_FE):latest' >> _deploy_fe.sh
	@echo 'sudo docker stop vicinity-frontend 2>/dev/null || true' >> _deploy_fe.sh
	@echo 'sudo docker rm vicinity-frontend 2>/dev/null || true' >> _deploy_fe.sh
	@echo 'sudo docker run -d --name vicinity-frontend --network vicinity-net --restart unless-stopped -p 80:80 $(IMG_FE):latest' >> _deploy_fe.sh
	@echo 'echo "=== FRONTEND DEPLOYED ==="' >> _deploy_fe.sh
	@echo 'sudo docker ps --filter "name=vicinity" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"' >> _deploy_fe.sh
	gcloud compute scp _deploy_fe.sh $(INSTANCE):_deploy_fe.sh --project=$(GCP_PROJECT) --zone=$(ZONE)
	-gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
		--command="bash _deploy_fe.sh && rm _deploy_fe.sh"
	-@rm -f _deploy_fe.sh 2>/dev/null

deploy-af: _ensure-vm
	@echo "====== Deploying Airflow ======"
	@echo '#!/bin/bash' > _deploy_af.sh
	@echo 'set -e' >> _deploy_af.sh
	@echo 'if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sudo sh; fi' >> _deploy_af.sh
	@echo 'if ! sudo docker compose version >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y -qq docker-compose-plugin; fi' >> _deploy_af.sh
	@echo 'gcloud auth print-access-token | sudo docker login -u oauth2accesstoken --password-stdin https://$(AR_HOST)' >> _deploy_af.sh
	@echo 'sudo docker pull $(IMG_AF):latest' >> _deploy_af.sh
	@echo 'sudo docker tag $(IMG_AF):latest vicinity-airflow:latest' >> _deploy_af.sh
	@echo 'cd $$HOME/vicinity-project/airflow' >> _deploy_af.sh
	@echo 'mkdir -p logs plugins' >> _deploy_af.sh
	@echo 'SLACK=$$(grep -oP "^SLACK_WEBHOOK_URL=\K.*" $$HOME/.env 2>/dev/null || echo "")' >> _deploy_af.sh
	@echo 'sudo AIRFLOW_IMAGE_NAME=vicinity-airflow:latest AIRFLOW_UID=$$(id -u) SLACK_WEBHOOK_URL=$$SLACK docker compose down 2>/dev/null || true' >> _deploy_af.sh
	@echo 'sudo AIRFLOW_IMAGE_NAME=vicinity-airflow:latest AIRFLOW_UID=$$(id -u) SLACK_WEBHOOK_URL=$$SLACK docker compose up -d' >> _deploy_af.sh
	@echo 'echo "=== AIRFLOW DEPLOYED ==="' >> _deploy_af.sh
	@echo 'sudo docker ps --filter "name=airflow" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"' >> _deploy_af.sh
	gcloud compute scp _deploy_af.sh $(INSTANCE):_deploy_af.sh --project=$(GCP_PROJECT) --zone=$(ZONE)
	-gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
		--command="bash _deploy_af.sh && rm _deploy_af.sh"
	-@rm -f _deploy_af.sh 2>/dev/null


# =============================================================================
# AIRFLOW PORT TOGGLE
# =============================================================================

expose-af: _check-config
	@if gcloud compute firewall-rules describe vicinity-allow-airflow \
		--project=$(GCP_PROJECT) > /dev/null 2>&1; then \
		echo "Airflow :8081 — already open"; \
	else \
		gcloud compute firewall-rules create vicinity-allow-airflow \
			--project=$(GCP_PROJECT) --allow=tcp:8081 \
			--target-tags=$(VM_TAG) --description="Vicinity Airflow UI"; \
	fi
	@echo "  http://$$(gcloud compute instances describe $(INSTANCE) \
		--project=$(GCP_PROJECT) --zone=$(ZONE) \
		--format='get(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null):8081"

hide-af: _check-config
	@if gcloud compute firewall-rules describe vicinity-allow-airflow \
		--project=$(GCP_PROJECT) > /dev/null 2>&1; then \
		gcloud compute firewall-rules delete vicinity-allow-airflow \
			--project=$(GCP_PROJECT) --quiet; echo "  Airflow :8081 closed"; \
	else \
		echo "Airflow :8081 — already closed"; \
	fi


# =============================================================================
# VM
# =============================================================================

ssh: _ensure-vm
	gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE)

status: _check-config
	@VM_STATUS=$$(gcloud compute instances describe $(INSTANCE) \
		--project=$(GCP_PROJECT) --zone=$(ZONE) \
		--format="get(status)" 2>/dev/null || echo "NOT_FOUND"); \
	VM_IP=$$(gcloud compute instances describe $(INSTANCE) \
		--project=$(GCP_PROJECT) --zone=$(ZONE) \
		--format="get(networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null || echo ""); \
	echo ""; \
	echo "====== $(INSTANCE) | $$VM_STATUS | $$VM_IP ======"; \
	echo ""; \
	if [ "$$VM_STATUS" = "RUNNING" ]; then \
		gcloud compute ssh $(INSTANCE) --project=$(GCP_PROJECT) --zone=$(ZONE) \
			--command="sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'" \
			2>/dev/null || echo "  (SSH failed)"; \
		echo ""; \
		curl -sf http://$$VM_IP:8000/ping > /dev/null 2>&1 \
			&& echo "  API       http://$$VM_IP:8000/docs   OK" \
			|| echo "  API       FAIL"; \
		curl -sf http://$$VM_IP > /dev/null 2>&1 \
			&& echo "  Frontend  http://$$VM_IP              OK" \
			|| echo "  Frontend  FAIL"; \
		curl -sf http://$$VM_IP:8001/mcp > /dev/null 2>&1 \
			&& echo "  MCP       http://$$VM_IP:8001/mcp     OK" \
			|| echo "  MCP       FAIL"; \
		if gcloud compute firewall-rules describe vicinity-allow-airflow \
			--project=$(GCP_PROJECT) > /dev/null 2>&1; then \
			echo "  Airflow   http://$$VM_IP:8081         OPEN"; \
		else \
			echo "  Airflow   internal only"; \
		fi; \
	else \
		echo "  VM is $$VM_STATUS — run 'make deploy'"; \
	fi
	@echo ""


# =============================================================================
# COMPOSITE
# =============================================================================

all: build push upload deploy deploy-af status

redeploy: build-api push-api deploy status

redeploy-fe: build-fe push-fe deploy-fe status

clean:
	rm -f vicinity-project*.tar.gz _deploy.sh _deploy_af.sh _deploy_fe.sh
	docker compose -f docker/docker-compose.yml down 2>/dev/null || true