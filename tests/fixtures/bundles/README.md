# Golden bundles (sintéticos)

Hay diez bundles `directory_v1` del escenario K3 del corpus sintético, uno por versión del motor:

- **`K3-engine-0.10.0/`** es el golden actual, evaluado con `invaria-engine@0.10.0` (segunda parte). Se generó el 2026-10-08 con `uv run --locked invaria bundle tests/fixtures/corpus/subscription-synthetic K3 tests/fixtures/bundles/K3-engine-0.10.0`. sha256 de su `manifest.json`: `d612eb4ed9b1a951b9d2a94558d2f3f82599113899dfeef79f7156abcdf4ba64`. Frente a 0.9.0 solo cambian `engine_ref` y el identificador: el perfil 1.0.0 no declara las reglas nuevas.
- **`K3-engine-0.9.0/`** es **histórico**: `invaria-engine@0.9.0`, retirado por la regla de completitud (0.10.0); se reproduce y se reconstruye byte a byte con su compatibilidad. Se generó el 2026-10-08 con:

      uv run --locked invaria bundle tests/fixtures/corpus/subscription-synthetic K3 tests/fixtures/bundles/K3-engine-0.9.0

  sha256 de su `manifest.json`: `3cbc4a18ccbec62c46ff28ca158c0600ceaf91eff257e68072dc92652115a033`. Frente a 0.8.0 solo cambian `engine_ref` y el identificador de la evaluación. El perfil del corpus (1.0.0) no declara `absence_needs_chain_scope`, así que resultado, controles y premisas son los mismos.
- **`K3-engine-0.8.0/`** es **histórico**: `invaria-engine@0.8.0`, retirado por la regla de ausencia con alcance (0.9.0). Se reproduce con su implementación de compatibilidad, que lo reconstruye byte a byte. Se generó el 2026-10-08 con:

      uv run --locked invaria bundle tests/fixtures/corpus/subscription-synthetic K3 tests/fixtures/bundles/K3-engine-0.8.0

  sha256 de su `manifest.json`: `96571b25184a514ae6402ef92ff91a4d96a42a0ac2675a2e5078e1b5662e4eb7`. Frente a 0.7.0, reescribe la premisa muxed, añade la de procedencia y declara `evidence_mapping_refs`. Resultado y controles no cambian.
- **`K3-engine-0.7.0/`** es **histórico**: `invaria-engine@0.7.0`, retirado por 0.8.0. Se reproduce con su implementación de compatibilidad, que lo reconstruye byte a byte. sha256 de su `manifest.json`: `aa28dbccc4c71345481d639bb74fb570624b95b1e0fac2a04cb9ab4cc251f188`.
- **`K3-engine-0.6.0/`** es **histórico**: `invaria-engine@0.6.0`, retirado por 0.7.0. Se reproduce con su implementación de compatibilidad, que lo reconstruye byte a byte. Se generó el 2026-10-07 con:

      uv run --locked invaria bundle tests/fixtures/corpus/subscription-synthetic K3 tests/fixtures/bundles/K3-engine-0.6.0

  sha256 de su `manifest.json`: `3f634c702b6a69964897560173962f72b585cd1be542639bce8529280749caa5`
- **`K3-engine-0.5.0/`** es **histórico**: `invaria-engine@0.5.0`, retirado por 0.6.0. Se reproduce con su implementación de compatibilidad, que lo reconstruye byte a byte; eso no demuestra la equivalencia con el código de 0.5.0 en todas las entradas.

  sha256 de su `manifest.json`: `69c8c2f924b26c8a2f8ba528e12385af725597cfa9729bab2dfd23fa07471c21`
- **`K3-engine-0.4.0/`** es **histórico**: `invaria-engine@0.4.0`, retirado por 0.5.0. Se reproduce con su implementación de compatibilidad, que lo reconstruye byte a byte; eso no demuestra la equivalencia con el código de 0.4.0 en todas las entradas.

  sha256 de su `manifest.json`: `7d574c8d89042f9435562a47061849c541d0de4af2c1bc2aeef6da4b47fe73fd`
- **`K3-engine-0.3.0/`** es **histórico**: `invaria-engine@0.3.0`, retirado por 0.4.0. Se reproduce con su implementación de compatibilidad, que lo reconstruye byte a byte.

  sha256 de su `manifest.json`: `da810a88da49377cf6128ee05bc1d6e631f39a7444c55bdc6390ef2cd560d6ed`
- **`K3-engine-0.2.0/`** es **histórico**: `invaria-engine@0.2.0`, retirado por 0.3.0. Se reproduce con la implementación de compatibilidad de 0.2.0, que lo reconstruye byte a byte.

  sha256 de su `manifest.json`: `bd74fa58ca71108ee395e634bd303f135b0033bfbe0ac1215b5c2a1d0b42367d`
- **`K3/`** es el golden **histórico**, grabado con `invaria-engine@0.1.0`, hoy retirado. Se conserva como evidencia: se reproduce con 0.1.0 (`engine_status: retired`) y 0.1.0 lo reconstruye byte a byte.

  sha256 de su `manifest.json`: `bec6969130fed2f8d64ca92c65fb6c4808c5d5beae65e83bd1283cd61c7b447f`

Entre los seis son iguales el resultado (BREAK), los controles, `snapshot.json`, `evidence.json` y `profile.json`. Cambian `versions.engine_ref`, `evaluation_id` (`eval-be1923e6…` → `eval-ce2ef2ae…` → `eval-cd2ada80…` → `eval-b98ab0f4…` → `eval-23819823…` → `eval-00812ffa…`) y `assumptions`, y con ellos los hashes de `evaluation.json` y del manifest. Cambios en `assumptions`:
- +2 en 0.2.0 y +1 en 0.3.0;
- en 0.4.0 se reformula el del clawback y se añade uno;
- +1 en 0.5.0;
- 0.6.0 parte de los 9 de 0.4.0 y añade 3 de integridad. No lleva el de cuarentena por control, porque el perfil de K3 no acota su cuarentena.

`tests/bundle/test_bundle.py` exige, para cada uno, que se siga reproduciendo (`REPRODUCED`, resultado financiero BREAK) con su motor exacto y que sea idéntico byte a byte a uno generado de nuevo con ese mismo motor.

Si cambia el motor, un contrato o el formato, esa prueba falla a propósito. Regenerar el bundle es entonces una decisión explícita: hay que versionar el motor y revisar el cambio de resultado.

El bundle no está firmado. El sha256 del manifest solo ancla la confianza si se recibe por un canal independiente.

## Rescate 0.4.0 congelado (`redemption-0.4.0/`)
`RD-CANCELLED` y `RD-SHORT-PAY`, evaluados con `invaria-redemption-engine@0.4.0`, retirado. Se congelaron el 2026-10-07 con la emulación del código actual (`inv013=False`) para detectar cualquier deriva futura. No prueban la equivalencia con el código anterior, del que no hay historia Git. Sus resultados coinciden con los esperados del corpus. Manifests: `a709355b…e007` y `11ad2218…341e` (`tests/bundle/test_engine_versions.py`).

## Rescate 0.5.0 congelado (`redemption-0.5.0/`)
`RD-CANCELLED` y `RD-SHORT-PAY` del corpus actual (perfil 1.4.0), evaluados con `invaria-redemption-engine@0.5.0` el 2026-10-07, justo antes de que el incremento 3 lo retirara (motor actual 0.6.0). Su implementación de compatibilidad (el código actual sin la regla de participantes no identificados) los reproduce y los reconstruye byte a byte. Con 0.6.0 dan el mismo resultado, porque no contienen efectos on-chain. Manifests: `361685f3…5fad` y `c7b68a65…e97d` (`tests/bundle/test_engine_versions.py`). Se generaron con `build_bundle(..., evaluate(inputs), mode=query_mode)` sobre `tests/fixtures/corpus/redemption-synthetic-1.4.0`.

## Rescate 0.6.0 congelado (`redemption-0.6.0/`)
`RD-CANCELLED`, `RD-PAID` y `RD-SHORT-PAY` del corpus de perfil 1.4.0, evaluados con `invaria-redemption-engine@0.6.0` el 2026-10-07. Se congelaron con el código de 0.6.0 **antes** de que se retirara (motor actual 0.7.0). Su implementación de compatibilidad, el código actual sin las reglas de 0.7.0, los reproduce y los reconstruye byte a byte. En esas entradas, que no tienen cuarentena, 0.7.0 da el mismo resultado y los mismos controles. Manifests: `7a336dff…4d0d`, `6446eaf5…139d` y `56ea0e96…89f3` (`tests/bundle/test_engine_versions.py`). Se generaron con `build_bundle(..., evaluate(inputs), mode=query_mode)` sobre `tests/fixtures/corpus/redemption-synthetic-1.4.0`.

## Rescate 0.7.0 congelado (`redemption-0.7.0/`)
`RD-CANCELLED`, `RD-PAID` y `RD-SHORT-PAY` del corpus de perfil 1.5.0, evaluados con `invaria-redemption-engine@0.7.0` el 2026-10-07. Se congelaron con el código de 0.7.0 **antes** de que se retirara (motor actual 0.8.0).

Su implementación de compatibilidad, el código actual sin las reglas muxed de 0.8.0, los reproduce y los reconstruye byte a byte. En esas entradas, sin movimientos muxed, 0.8.0 da el mismo resultado y los mismos controles.

Manifests: `ec6c6be4…b125`, `3d283d61…a586` y `60c42a75…6dfe` (`tests/bundle/test_engine_versions.py`). Se generaron con `build_bundle(..., evaluate(inputs), mode=query_mode)` sobre `tests/fixtures/corpus/redemption-synthetic-1.5.0`.

## Rescate 0.10.0 congelado (`redemption-0.10.0/`)
`RD-CANCELLED` (UNKNOWN por la regla de ausencia con alcance), `RD-PAID` y `RD-SHORT-PAY` del corpus de perfil 1.6.0, evaluados con `invaria-redemption-engine@0.10.0` el 2026-10-08. Se congelaron **antes** de la segunda parte (motor actual 0.11.0). Su compatibilidad los reproduce y los reconstruye byte a byte; bajo 1.6.0, 0.11.0 da el mismo resultado, controles y premisas.

Manifests: `8db23771…ac51`, `abac8f95…5c56` y `cb284a94…7ceb` (`tests/bundle/test_engine_versions.py`).

## Rescate 0.9.0 congelado (`redemption-0.9.0/`)
`RD-CANCELLED`, `RD-PAID` y `RD-SHORT-PAY` del corpus de perfil 1.5.0, evaluados con `invaria-redemption-engine@0.9.0` el 2026-10-08. Se congelaron con el código de 0.9.0 **antes** de que se retirara (motor actual 0.10.0). Se generaron con `build_bundle(..., evaluate(inputs), mode=query_mode)`.

Su implementación de compatibilidad, el código actual sin la regla de 0.10.0, los reproduce y los reconstruye byte a byte. Bajo el perfil 1.5.0, que no declara la regla, 0.10.0 da el mismo resultado, los mismos controles y las mismas premisas. Bajo el perfil 1.6.0, RD-CANCELLED sería UNKNOWN.

Manifests: `76572f67…3cf`, `4cceaab6…d5b` y `35b7c6e5…f1f` (`tests/bundle/test_engine_versions.py`).

## Rescate 0.8.0 congelado (`redemption-0.8.0/`)
`RD-CANCELLED`, `RD-PAID` y `RD-SHORT-PAY` del corpus de perfil 1.5.0, evaluados con `invaria-redemption-engine@0.8.0` el 2026-10-08. Se congelaron con el código de 0.8.0 **antes** de que se retirara (motor actual 0.9.0). Su implementación de compatibilidad los reproduce y los reconstruye byte a byte. En esas entradas 0.9.0 da el mismo resultado y los mismos controles, y además declara `evidence_mapping_refs`. Manifests: `d16fb5c2…9ceb5`, `fb09c81f…efb72b` y `e4f05275…deac67` (`tests/bundle/test_engine_versions.py`).
