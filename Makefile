PY ?= python3
export PYTHONPATH := src

.PHONY: install test data close eval all clean

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest

data:
	$(PY) -m rlc.cli generate --config config.yaml

close:
	$(PY) -m rlc.cli close --config config.yaml

eval:
	$(PY) -m rlc.cli evaluate --config config.yaml

all: data close eval

clean:
	rm -rf out/* data/generated .pytest_cache
	touch out/.gitkeep
