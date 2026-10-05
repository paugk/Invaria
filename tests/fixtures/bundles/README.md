# Golden bundles (sintéticos)

`K3/` es el bundle `directory_v1` del escenario K3 del corpus sintético, generado con:

    uv run --locked invaria bundle tests/fixtures/corpus/subscription-synthetic K3 tests/fixtures/bundles/K3

sha256 de `K3/manifest.json`: `bec6969130fed2f8d64ca92c65fb6c4808c5d5beae65e83bd1283cd61c7b447f`

`tests/bundle/test_bundle.py` exige dos cosas:
- que este bundle se siga reproduciendo (`REPRODUCED`, resultado financiero BREAK);
- que sea idéntico byte a byte a uno generado de nuevo.

Si cambia el motor, un contrato o el formato, esa prueba falla a propósito. Regenerar el bundle es entonces una decisión explícita: hay que versionar el motor y revisar el cambio de resultado.

El bundle no está firmado. El sha256 del manifest solo ancla la confianza si se recibe por un canal independiente.
