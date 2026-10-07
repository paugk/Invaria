# Fixtures Stellar testnet

Hay dos tipos de datos reales de testnet:
- muestras de **terceros** (USDC), descritas en las secciones siguientes;
- **nuestra emisión propia** de DEMOA (`demoa-own`), descrita al final.

**Las muestras USDC son datos reales y públicos de terceros en Stellar testnet. No son nuestras y no tienen relación con la suscripción sintética DEMO-A ni con `SUB-0001`.** Ninguna observación de esas muestras tiene `operation_ref`: vincular un efecto on-chain a una operación exige un `ExecutionLink` explícito y aprobado, y aquí no existe ninguno. El corpus sintético (`tests/fixtures/corpus/`) usa cuentas y hashes derivados de etiquetas `invaria:synthetic:*`, que no existen en la red.

## Captura
- **Fecha:** 2026-10-05T00:41 UTC. Cada intercambio guarda su propio `captured_at`.
- **Endpoints:**
  - Horizon `https://horizon-testnet.stellar.org` (horizon 29.0.0, protocolo 29);
  - RPC `https://soroban-testnet.stellar.org`.
- **Red verificada en ambos:** `Test SDF Network ; September 2015`.
- **Formato** (`recordings/<muestra>/`): por cada petición, `<sha256(método, URL y cuerpo)>.json` (método, URL, cuerpo, status, `response_sha256`, `captured_at`) y `.body` con los bytes exactos de la respuesta. `ReplayClient` rechaza un cuerpo cuyo sha256 no coincida o una petición no grabada.
- **Comando de captura** (`$NOW` = la fecha de arriba):
  `uv run --locked invaria stellar ingest --target tests/fixtures/stellar/targets/<muestra>.json --start-ledger A --end-ledger B --store /tmp/store --sac --page-limit 2 --sac-page-limit 200 --recorded-at $NOW --record tests/fixtures/stellar/recordings/<muestra>`

## Activo
USDC de testnet:
- **Emisor:** `GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5`, `credit_alphanum4`.
- **Contrato SAC:** `CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA`. Se derivó localmente (`invaria.stellar.xdr.sac_contract_id`) y coincide con el `contract_id` que publica Horizon `/assets`.

## Muestras
- **`usdc-gclcz`**: cuenta `GCLCZEQZ2THTEDAOFI66LACNPLY4OBKN7VKLEZFMBIHYKYQOW2W7T3Z6`, ledgers 5024520–5024600.
  - Ruta Horizon: 6 pagos `payment` USDC, todos exitosos, leídos en 4 páginas de 2.
  - Ruta RPC: 109 eventos del contrato. 5 son los mismos efectos que los pagos Classic (eventos unificados, CAP-67) y cuentan una sola vez.
  - La transacción `b0725d99…` tiene memo `id`: su evento lleva `{amount, to_muxed_id: u64}` y se excluye como `MUXED_ACCOUNT`, porque no se distingue de un destino muxed.
  - Hashes de las transacciones de los pagos:
    - `776ea195…8b81` (ledger 5024525)
    - `8de0e90e…c098` (5024539)
    - `7902c7ee…481e` (5024552)
    - `5ac79e26…9e8f` (5024567)
    - `b0725d99…f892` (5024582)
    - `96032753…2be8` (5024593)
- **`usdc-issuer`**: el emisor, ledgers 5015930–5015945.
  - 1 pago de 20,0000000 USDC hacia el emisor (tx `b1c46a5e…59d2`, ledger 5015939).
  - En RPC es el evento `burn` del mismo efecto: un solo efecto.
- **`usdc-gb4mm`**: cuenta `GB4MMSZ5FY3KOCMMN77DNJBSKXFZVRXMLM5SKKDIVGTWGR55DKJM7GSD`, ledgers 5000475–5000490.
  - Un `path_payment_strict_receive` (tx `fbab5a26…24d0`), que Horizon excluye porque no está soportado todavía.
  - RPC muestra sus dos efectos sobre USDC: `mint` del emisor a GB4MM y `transfer` de GB4MM a `GAYF33…`.

Los hashes completos están en las grabaciones y se verifican en `tests/stellar/test_adapter.py`.

## Emisión propia: `demoa-own` (creada el 2026-10-05 con autorización de la responsable)

| Elemento | Valor |
|---|---|
| Emisor `DEMOA` (`credit_alphanum12`) | `GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP` |
| Inversor de prueba | `GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK` |
| SAC derivado | `CC5E43MS34OKBBNBK7X56CZDTDODKJ3NO3O2UDFCNQ2FFQLK74EGFDRS`. No está desplegado: Horizon no publica `contract_id`, pero testnet emite igualmente los eventos unificados con este id. |

**Claves.** Se generaron con Stellar CLI 28.1.0 en el almacén seguro del sistema (`stellar keys ... --secure-store`) y no están en el repositorio. Ambas cuentas se fondearon con friendbot.

Transacciones (ledgers 5027650–5027672):

| Ledger | Tx | Qué es | Resultado esperado y observado |
|---|---|---|---|
| 5027652 | `335a5271…a713` | Trustline del inversor a DEMOA | No es un movimiento |
| 5027658 | `87835238…99a2` | Pago del emisor al inversor de 1.000,0000000 DEMOA, memo `SUB-0001` | Enlazado a `SUB-0001` **solo** por `links/demoa-own.json`. RPC: evento `mint` con `to_muxed_id` texto, mismo efecto. |
| 5027664 | `29bca570…6126` | Inversor → emisor, 2.000 DEMOA con saldo 1.000 | **Fallida** (`op_underfunded`, comisión cobrada): `tx_successful=false`, sin efecto y sin evento. |
| 5027669 | `00fd0148…34bf` | Segundo pago de 1.000 DEMOA con el mismo memo | **Sin enlace:** el memo no enlaza. RPC: `mint`, mismo efecto. |

El enlace explícito (`links/demoa-own.json`, `approval-owner-2026-10-05-testnet-demo`) es el único motivo por el que T1 lleva `operation_ref = SUB-0001`. La institución, el banco y el TA de `SUB-0001` siguen siendo sintéticos; esta emisión no representa un fondo real.

## Regrabación de `getEvents` (2026-10-06)
El `endLedger` de RPC `getEvents` es exclusivo, y el adaptador ahora pide `end_ledger + 1`; antes, el último ledger de cada rango no se leía por RPC aunque la cobertura lo incluía. Eso cambia la petición, así que en `usdc-gclcz`, `usdc-issuer`, `usdc-gb4mm` y `demoa-own` se regrabaron **solo** los intercambios `getEvents`, con su propio `captured_at`. Para hacerlo se volvió a capturar cada muestra en un directorio temporal:
- las páginas de Horizon y los ledgers salieron byte a byte idénticos a los grabados;
- `/assets` y la raíz `/` sí cambiaron, porque son estadísticas vivas, y se conservaron las originales.

El único evento nuevo en un último ledger es un `approve` de `usdc-issuer` (ledger 5015945), que no es un movimiento.

## Límites
- Testnet se reinicia periódicamente. Las grabaciones permiten reproducir todo offline; la prueba real (`INVARIA_LIVE_TESTNET=1`) se omite si la historia ya no contiene la muestra.
- La cobertura es `provider_claimed`: es lo que Horizon y RPC devuelven, sin validación criptográfica de checkpoints ni fuente independiente.
