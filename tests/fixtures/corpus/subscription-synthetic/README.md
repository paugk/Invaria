# Corpus sintético: suscripción DEMO-A

**Todo en esta carpeta es sintético.** Ningún dato procede de una institución, banco, TA ni de la red Stellar. Las cuentas Stellar son StrKeys válidas derivadas de `sha256("invaria:synthetic:account:...")`. Los hashes de transacción son `sha256("invaria:synthetic:tx:<etiqueta>")`. Los ledgers son números inventados. Ninguno corresponde a una transacción de testnet obtenida o verificada. El test `tests/contracts/test_corpus.py` recalcula estas derivaciones.

Las decisiones del perfil son de este fixture, no políticas de fondos reales: coincidencia exacta, sin comisiones, sin redondeo, sin tolerancia, y quién es la autoridad de cada tipo de hecho.

## Caso

La orden `SUB-0001` es de **1.000 participaciones a USD 100,00**, con un **importe esperado de USD 100.000,00** y sin comisiones.

| Concepto | Valor exacto |
|---|---|
| Participaciones | `{"atoms":"10000000000","scale":7,"unit":"FUND_SHARE"}` |
| Importe | `{"atoms":"10000000","scale":2,"unit":"USD"}` |
| Precio | `{"atoms":"10000","scale":2,"unit":"USD"}` por participación |
| Instrumento | `syn:fund:DEMO-A:class-a` (unidad `FUND_SHARE`, escala 7) |
| Representación | Stellar Classic `DEMOA`, emisor `GDMYHWUG…CZVZ`, `stellar:testnet`; relación 1:1, 7 decimales |

Un `raw_sha256` demuestra la integridad de los bytes recibidos, no que su contenido sea verdadero ni que la fuente esté completa. Para eso existen la autoridad por tipo de hecho y los certificados de cobertura.

## Fuentes y autoridad (por tipo de hecho)

| Fuente | Autoridad para | Mapping |
|---|---|---|
| `oms-synthetic` | `order_accepted` | `oms-csv-synthetic@1.0.0` |
| `bank-synthetic` | `cash_settled` | `bank-csv-synthetic@1.0.0` |
| `ta-synthetic` | `units_registered` | `ta-csv-synthetic@1.0.0` |
| `stellar-testnet-frozen` | `token_movement` (solo la ejecución técnica) | `stellar-frozen-synthetic@1.0.0` |

La correlación es explícita:
- **Registros institucionales:** `operation_ref = order_ref`.
- **Movimientos on-chain:** el memo de la transacción enlaza con la orden. Importe y hora no bastan para establecer identidad.
- **Cuentas:** se enlazan mediante un vínculo de identidad aprobado (`identity_links.json`).

La cobertura exigida es de nivel `internally_checked`, sin huecos ni filas en cuarentena, desde la hora de la orden hasta `valid_at`.

## Archivos

| Archivo | Contenido |
|---|---|
| `profile.json` | Perfil de operación: fuentes, cobertura, controles, precedencia, política de evidencia y capacidades no soportadas. |
| `raw/` | Bytes crudos (CSV y JSON congelado). Cada observación cita su `raw_sha256` y su localizador. |
| `mappings/` | Mappings CSV versionados. Importar los CSV de `raw/` con ellos reproduce las observaciones institucionales y los certificados de cobertura (`tests/ingest/`). |
| `observations/<timeline>.json` | Registro de entregas de cada línea temporal. Las correcciones y retiradas son revisiones nuevas con `supersedes`; nada se edita. Las observaciones comunes son idénticas entre líneas temporales. |
| `coverage.json` | Certificados de cobertura por fuente e intervalo. |
| `scenarios.json` | Snapshots cerrados y **resultados esperados escritos antes de que exista un evaluador** (el evaluador no está implementado), con su justificación. |
| `bundle_manifest_K2.draft.json` | Manifest inicial del paquete de evidencia de K2: hashes de artefactos. Firma, exportación y replay: `not_implemented`. |

## Escenarios

| ID | Esperado | Por qué |
|---|---|---|
| K1 | UNKNOWN | Faltan las observaciones y la cobertura de banco y TA. Ausencia no es MATCH. |
| K2 | MATCH | Los cuatro controles obligatorios son iguales en atoms, con cobertura completa. |
| K3 | BREAK | La corrección bancaria (rev. 2) sustituye la confirmación anterior: USD 99.500,00 frente a 100.000,00. Delta de `-50000` atoms (USD −500,00), equivalente a 5 participaciones. |
| K2-historical | MATCH | Snapshot de K2 consultado después de K3. La corrección B2 se registró después de `known_at` y no pertenece al snapshot. |
| RETRACTION | UNKNOWN | El banco retira su única confirmación sin reemplazo. Hay pérdida de soporte, pero eso no demuestra impago. |
| DUPLICATE-DELIVERY | MATCH | La misma observación Stellar se entrega dos veces y produce un solo efecto: 1.000, no 2.000. |
| SOURCE-CONFLICT | UNKNOWN | Dos entregas con la misma clave y revisión traen importes distintos. No se elige ninguna en silencio. |
| AMBIGUOUS-IDENTITY | UNKNOWN | Dos movimientos de 1.000 sin enlace de ejecución. No se resuelve la identidad por importe ni por hora. |
| AMBIGUOUS-SCALE | UNKNOWN | La fila del TA trae `1000` sin los 7 decimales declarados, así que va a cuarentena. No se adivina la escala. |

Precedencia de resultados (sobre los controles obligatorios aplicables):
1. Si algún control da FAIL, el resultado es BREAK.
2. Si todos dan PASS, el resultado es MATCH.
3. En cualquier otro caso, UNKNOWN.

Un error técnico cuenta como UNKNOWN, nunca como PASS.

`effective_observation_ids` lista las observaciones que sostienen los controles una vez aplicadas la deduplicación, las sustituciones, las retiradas, los conflictos y la ambigüedad.

## Capacidades no soportadas por este perfil

Si aparece alguna, el resultado es UNKNOWN / `UNSUPPORTED_CAPABILITY`, nunca una aproximación:
- `fees`, `partial_fill`, `multiple_cash_payments`, `variable_nav`, `fx_conversion`, `rounding`
- `omnibus_account`, `distributor_delivery`
- `sac_events`, `soroban_custom_token`
- `redemption`
- `amount_time_matching`
