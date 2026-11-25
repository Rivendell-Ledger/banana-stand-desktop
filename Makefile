PY := .venv/bin/python

.PHONY: install run test doctor backup migrate clean

install:
	python3 -m venv --system-site-packages .venv
	.venv/bin/pip install -q -r requirements.txt
	mkdir -p data backups logs secret
	chmod 700 secret

run:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8093 --proxy-headers

test:
	$(PY) -m pytest -q

doctor:
	banana doctor

backup:
	banana backup

migrate:
	banana migrate

clean:
	rm -rf .pytest_cache app/__pycache__ app/*/__pycache__ tests/__pycache__
