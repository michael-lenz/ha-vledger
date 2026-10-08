# ha-vledger

A vehicle ledger for Home Assistant: a raw log of every state a vehicle
reports, and trips, charging sessions, refuellings, consumption and cost
derived from it — passively, from any integration that exposes the vehicle,
without a line of manufacturer-specific code.

**Status:** structure only. Nothing captures or derives yet; the
requirements live in the project's register, `ha-vledger-pm`.

## Layout

One repository, two packages, one version number (ADR-0002):

```
src/vledger/               the library and the vledger CLI — all derivation logic,
                           no Home Assistant, no dependencies; published to PyPI
custom_components/vledger/ the Home Assistant integration, a shell over the library;
                           pins vledger by the same version in manifest.json
tests/                     pytest; library tests with L0 fixtures, integration tests
                           with pytest-homeassistant-custom-component
```

## Developing

```bash
pip install -e ".[dev]"        # library and CLI
pip install -e ".[dev,ha]"     # plus the Home Assistant test stack, for the integration
python -m pytest
ruff check src tests custom_components
```

The integration's manifest pins a library version that may not be on PyPI
yet; Home Assistant skips the install when the package already imports, so
a development instance needs the editable install above first.

## License

BSD-3-Clause — see [LICENSE](LICENSE).
