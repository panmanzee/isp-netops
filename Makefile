# Lab workflow. `make e2e` builds, deploys, verifies, proves drift detection, and tears down.
PY ?= .venv/bin/python
ANSIBLE ?= ansible-playbook
TOPO = build/topology.clab.yml

.PHONY: help key image render up deploy verify backup drift watch down test e2e monitor-up monitor-down vendor-sim vendor-backup vendor-drift vendor-e2e
help:
	@grep -E '^[a-z0-9-]+:' Makefile | cut -d: -f1 | tr '\n' ' '; echo

key:            ## SSH key the routers trust (generated once, never committed)
	@mkdir -p ansible/.ssh
	@test -f ansible/.ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -f ansible/.ssh/id_ed25519 -C netops-lab

image: key      ## build the router image
	docker/frr-lab/build.sh

render:         ## generate topology, configs, inventory from data/network.yml
	$(PY) -m netops validate
	$(PY) -m netops render

up: render      ## start the virtual network
	sudo containerlab deploy -t $(TOPO)

deploy:         ## push the intended configs with Ansible
	$(ANSIBLE) ansible/deploy.yml

verify:         ## BGP / OSPF health check
	$(PY) -m netops verify

backup:         ## save running configs (git history in backups/)
	$(PY) -m netops backup

drift:          ## compare running config with the source of truth and the last backup
	$(PY) -m netops drift

watch:          ## monitoring daemon (metrics on :9108, alerts)
	$(PY) -m netops watch

down:           ## destroy the virtual network
	sudo containerlab destroy -t $(TOPO) --cleanup

test:           ## unit tests (no network needed)
	$(PY) -m pytest -q

e2e: image up deploy
	scripts/e2e.sh
	$(MAKE) down

monitor-up:     ## Prometheus + Grafana (needs GRAFANA_PASSWORD in .env)
	cd monitoring && docker compose --env-file ../.env up -d

monitor-down:
	cd monitoring && docker compose --env-file ../.env down

vendor-sim:     ## start the IOS and FortiOS stand-ins (ports 2201 and 8443), Ctrl-C to stop
	IOS_PASSWORD=$${IOS_PASSWORD:?set IOS_PASSWORD} FORTIOS_TOKEN=$${FORTIOS_TOKEN:?set FORTIOS_TOKEN} PYTHONPATH=. sh -c 'trap "kill 0" EXIT; $(PY) -m labsim.ios_sim & $(PY) -m labsim.fortios_sim & wait'

vendor-backup:  ## save the vendor targets' configs (git history in backups/vendor)
	$(PY) -m netops vendor-backup

vendor-drift:   ## compare the vendor targets with vendor/intended and the last backup
	$(PY) -m netops vendor-drift

vendor-e2e:     ## backup + drift against the stand-ins, no containers needed
	PY=$(PY) bash scripts/vendor_e2e.sh
