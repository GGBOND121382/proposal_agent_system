.PHONY: run validate test
run:
	bash scripts/run.sh
validate:
	bash scripts/validate.sh
test:
	pytest -q
