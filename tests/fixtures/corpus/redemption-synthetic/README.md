# Corpus HISTÓRICO sintético: rescate DEMO-A (perfil 1.3.0)

> **Histórico.** Perfil `fund-redemption-synthetic@1.3.0`; sus conclusiones se grabaron con `invaria-redemption-engine@0.4.0`, hoy retirado. Se conserva para reproducirlas, y aquí vive el generador de ambos corpus. El corpus actual es `../redemption-synthetic-1.4.0` (`build_corpus.py --profile=1.4.0`).

**Todo en esta carpeta es sintético.**
- Ningún dato procede de una institución, banco, TA, administrador ni de la red Stellar.
- Cuenta inversora y emisor: las StrKeys sintéticas del corpus de suscripción.
- Hashes de transacción: `sha256("invaria:synthetic:tx:<etiqueta>")`; ledgers inventados.

**Origen de los archivos.** Todo salvo este README lo genera `build_corpus.py`, que usa solo la biblioteca estándar y no importa el motor. Los resultados esperados están escritos a mano en ese script, a partir de la semántica aprobada. Una prueba exige que los archivos coincidan con su salida:
- regenerar: `python3 tests/fixtures/corpus/redemption-synthetic/build_corpus.py`;
- comprobar: `... --check`.

Regenerar es una decisión explícita, porque cambia los resultados esperados.

**Alcance.** Solo el perfil sintético `fund-redemption-synthetic@1.3.0`. No es política de ningún fondo ni se aplica a operaciones reales.

## Caso

La solicitud `RED-0001` del TA, aceptada el **2026-10-05T10:00Z** (secuencia **1005** del diario sintético `ta-journal-demoa`, asignada por la fuente), rescata **100 participaciones**: una cantidad fija, de una posición de 1.000 a la secuencia 1004.

| Concepto | Valor exacto |
|---|---|
| Unidades | `{"atoms":"1000000000","scale":7,"unit":"FUND_SHARE"}` |
| Precio aprobado (`price:DEMO-A:2026-10-04`) | `{"atoms":"10000","scale":2,"unit":"USD"}` |
| Pago esperado | `{"atoms":"1000000","scale":2,"unit":"USD"}` |
| Vencimiento | `accepted_at` + 48 h exactas = **2026-10-07T10:00Z** |
| Retirada | Burn al emisor, posterior a la aceptación, con enlace de ejecución |

## Fuentes, autoridad y cobertura

| Fuente | Autoridad | Alcance exigido |
|---|---|---|
| `ta-synthetic` | solicitud, cancelación, reactivación, posición, diario de posición y débito | cuenta |
| `bank-synthetic` | efectivo | cuenta y moneda (USD), sin filtros |
| `pricing-synthetic` | precio aprobado | — |
| `stellar-testnet-frozen` | burn, `provider_claimed` | — |

Los certificados `cov-<fuente>-s0…s4` cubren `[2026-10-04T00:00Z, corte)`. Los intervalos son semiabiertos.

Variantes deliberadas:
- `cov-bank-s3-partial`: termina un día antes.
- `cov-bank-s3-to-due`: termina en el vencimiento, que queda excluido.
- `cov-bank-s3-gap`: declara un hueco.
- `cov-bank-s2-partial`: corta antes de `valid_at`.
- `cov-ta-s2-late-start`: empieza después de la posición antigua.
- `cov-bank-s3-filtered-ok`: filtrado por la cuenta y USD (compatible).
- `cov-bank-s3-filtered-status`: filtrado por estado (incompatible).

**Formato de los crudos.** Los crudos son JSON congelado (`raw/<id>.json`, `synthetic: true`) y no hay mappings CSV. La verificación R1 da `REPRODUCED`; la R2 da `INCOMPLETE`.

## Cortes

| Corte | Instante (= `valid_at` = `known_at` = reloj, salvo los escenarios de corte económico) |
|---|---|
| S0 | 2026-10-05T11:30Z |
| S1 | 2026-10-06T09:00Z, antes del vencimiento |
| S2 | 2026-10-06T16:00Z |
| S3 | 2026-10-07T12:00Z, después del vencimiento |
| S4 | 2026-10-08T10:00Z |

## Escenarios (46)

| Grupo | Escenarios y resultado |
|---|---|
| Plazo | `RD-PENDING` UNKNOWN; `RD-PAID` MATCH; `RD-PAID-AT-DUE` MATCH; `RD-MISSED` BREAK; `RD-MISSED-REPLAYED` BREAK; `RD-MISSED-NO-COVERAGE` UNKNOWN; `RD-MISSED-COVERAGE-TO-DUE` UNKNOWN; `RD-MISSED-BANK-GAP` UNKNOWN; `RD-LATE` BREAK; `RD-BAD-DUE` UNKNOWN; `RD-BAD-DUE-MISSED` BREAK |
| Importes | `RD-SHORT-PAY` BREAK; `RD-PRICE-MISMATCH` BREAK; `RD-INEXACT` UNKNOWN; `RD-DUPLICATE` MATCH |
| Posición | `RD-OVER-POSITION` BREAK; `RD-POSITION-RECONSTRUCTED` MATCH; `RD-POSITION-SHORT` BREAK; `RD-POSITION-STALE` UNKNOWN; `RD-POSITION-AMBIGUOUS` UNKNOWN |
| Burn | `RD-BURN-BEFORE-ACCEPTANCE` UNKNOWN |
| Cancelación | `RD-CANCELLED` MATCH (cancelled); `RD-CANCELLED-NO-COVERAGE` UNKNOWN (cancelled); `RD-CANCELLED-PAID` y `-BURNED` BREAK (cancelled); `RD-CANCELLED-AFTER-DUE` BREAK (cancelled); `RD-CANCEL-KNOWN-LATE-BEFORE` BREAK y `-AFTER` MATCH (cancelled); `RD-CANCEL-UNAUTHORIZED` UNKNOWN |
| Retractación | `RD-CANCEL-RETRACTED-BEFORE` MATCH (cancelled) y `-AFTER` MATCH; `RD-CANCEL-RETRACT-CONFLICT` UNKNOWN; `RD-REACTIVATED` UNKNOWN; `RD-RETRACTED` UNKNOWN; `RD-RETRACTED-PAID` UNKNOWN (el pago sigue efectivo y citado) |
| Destinatario y pagos sin vínculo | `RD-WRONG-RECIPIENT` BREAK; `RD-UNLINKED-CANDIDATE` UNKNOWN; `RD-UNLINKED-NOT-CANDIDATE` BREAK |
| Corte económico y correcciones | `RD-PAID-AFTER-ECONOMIC-CUT` UNKNOWN; `RD-MISSED-CUT-BEFORE-DUE` UNKNOWN; `RD-SHORT-PAY-BEFORE-CORRECTION` BREAK; `RD-CORRECTION-KNOWN-LATE` MATCH |
| Filtros de cobertura | `RD-MISSED-FILTER-COMPATIBLE` BREAK; `RD-MISSED-FILTER-INCOMPATIBLE` UNKNOWN |
| Burn anticipado tras cancelar y colisiones | `RD-CANCELLED-EARLY-BURN` BREAK (cancelled); `RD-SETTLEMENT-REF-COLLISION` UNKNOWN |

La justificación de cada escenario está en su `rationale` (`scenarios.json`).
