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
  - La transacción `b0725d99…` tiene memo `id` 732207663626: su evento lleva `{amount, to_muxed_id: u64}`. Hasta el incremento 3 se excluía como `MUXED_ACCOUNT`, porque el evento solo no distingue un destino muxed. Desde el incremento 4, el pago Classic muestra que el receptor no es muxed y que su memo id es ese valor, así que corrobora como memo y no queda nada en cuarentena.
  - Hashes de las transacciones de los pagos:
    - `776ea195…8b81` (ledger 5024525)
    - `8de0e90e…c098` (5024539)
    - `7902c7ee…481e` (5024552)
    - `5ac79e26…9e8f` (5024567)
    - `b0725d99…f892` (5024582)
    - `96032753…2be8` (5024593)
- **`usdc-gbiujq-muxed`** (grabada el 2026-10-07): cuenta `GBIUJQGXQNAHC6ZQR2AHIYNLDF24C63NZUQ7DUMNQUQQYHPFBY4NQCQX`, USDC del emisor `GDXARTTX…TJUE` (otro USDC de testnet), ledgers 5072600–5072610.
  - Un pago de 3 USDC de `GB4QBX…` a la sub-cuenta muxed `MBIUJQ…ACK` (id 310350723), tx `480bb59a…0cdc`, sin memo.
  - Su evento SAC lleva `to_muxed_id` u64 310350723 y corrobora el pago Classic, sin cuarentena.
  - Se encontró leyendo 300 páginas recientes de `/payments` (solo lectura). El titular de la sub-cuenta es desconocido.
- **`usdc-issuer`**: el emisor, ledgers 5015930–5015945.
  - 1 pago de 20,0000000 USDC hacia el emisor (tx `b1c46a5e…59d2`, ledger 5015939).
  - En RPC es el evento `burn` del mismo efecto: un solo efecto.
- **`usdc-gb4mm`**: cuenta `GB4MMSZ5FY3KOCMMN77DNJBSKXFZVRXMLM5SKKDIVGTWGR55DKJM7GSD`, ledgers 5000475–5000490.
  - Un `path_payment_strict_receive` USDCAllow → USDC (tx `fbab5a26…24d0`, fee bump), de GB4MM a `GAYF33…`.
  - Se registra como `chain_effect`: un crédito Classic de 34 567 USDC para GAYF33 y dos legs SAC (`mint` del emisor a GB4MM y `transfer` de GB4MM a GAYF33). La correspondencia por neto queda `corroborated`.
- **`usdc-gcdkpp`** (capturada el 2026-10-06): cuenta `GCDKPP3V4VIYNR5STCILS25I65KBM44TTJAIQ4HIBQFN2LEJ2HK5KLZX`, ledgers 5061229–5061233.
  - Un `path_payment_strict_receive` **fallido**, con USDC de origen (tx `c1959d21…8e25`). Horizon muestra `source_amount` 0,0000000 y 40 613 661,1519794 XLM pedidos; ninguno de los dos se ejecutó.
  - Un `path_payment_strict_receive` XLM → USDC exitoso (tx `994d6b2f…4c3c`, op 2), de `GDQLIJ…` a GCDKPP por 341,3500694 USDC. Uno de sus legs SAC tiene un liquidity pool como contraparte (`ScAddress` tipo 4), así que la correspondencia queda `sac_incomplete`.
  - Esta muestra destapó que el `endLedger` de RPC es exclusivo (ver «Regrabación»).
- **`usdc-gazy2f`** (capturada el 2026-10-06): cuenta `GAZY2FT4FU6G5Y5S64DBKMNAS5UT2NORQX77DLSV54EKILLMINVNRQCH`, ledgers 5061281–5061285.
  - Un `path_payment_strict_send` XLM → USDC de la cuenta a sí misma (tx `8b96897b…e257`): se envían 0,0000010 XLM y se reciben 0,0000009 USDC.
  - El leg del pool no se decodifica: `sac_incomplete`.
- **`usdc-gbain6`** (fills del DEX, capturada el 2026-10-07): cuenta `GBAIN6CHZJGBL365JNXSRQEKALXYTWKXANQZ3RBM7AGUEYYKLJJ6SNR6`, ledger 5069339.
  - Su oferta 894515 (vende LUSD, compra USDC) la cruza el `path_payment_strict_receive` de `GALBIMJM…` (tx `836d2133…ff95`). Recibe 0,2479821 USDC.
  - Horizon no lista la operación entre los pagos de GBAIN6. El adaptador lee `/transactions/{hash}/operations` y `/operations/{id}/effects`: `dex_fill` Classic y SAC, `corroborated`.
- **`usdc-issuer-fill`** (fills del DEX, capturada el 2026-10-07): el emisor de USDC (`GBBD47…`), ledger 5069194.
  - Su oferta 32 (vende USDC por USDCAllow) la cruza el path payment de GB4MM a GAYF33 (tx `ba0ab984…9362`). El lado SAC es un `mint` de 34 567 USDC que la primera versión del adaptador habría leído como movimiento. Ahora es un `dex_fill` (débito del emisor), `corroborated`.

Ambas se capturaron con `invaria stellar ingest --sac --page-limit 2 --record` (solo lecturas públicas).

**Clawback (incremento 2, capturadas el 2026-10-07).** Se localizaron filtrando con `getEvents` de RPC los eventos `clawback` de la ventana de retención: había 1039.
- **`testusb-holder-clawback`:** cuenta `GBV6VEMHOOLOAHERL4JO35X3IDLR4HQIZ3BTUYXFCAO4Z526COZMUJMN`, activo `TESTUSB:GCWSNDOOTAGUCTR4X62RCH5UOAUVSWV5VL2AZSCWBK27DHL2IFQDPHVX`, ledger 5070992.
  - Es un `clawback` Classic de 10,123 TESTUSB (tx `5202896d…b77f`), que Horizon no lista entre los pagos del titular.
  - El evento SAC y la operación Classic coinciden: `corroborated`.
- **`testusb-issuer-clawback`:** el mismo ledger, visto desde el emisor. En un mismo store, titular y emisor registran los mismos dos efectos, no cuatro.
- **`gdice-issuer-cb-clawback`:** emisor `GAYYH44SS4OSFDY4WXMWM2BKRA2RG5M6NZULUQWGHKGFASFY2ZVI7IHX` (`gdICE`, unidad interna `GDICE`), ledger 5061607.
  - Es un `clawback_claimable_balance`: la parte del evento es un claimable balance (`ScAddress` tipo 3).
  - Queda como `CLAWBACK_CLAIMABLE_BALANCE`, no soportado hasta el incremento 3, nunca como retirada de una cuenta.
- **`testusb-failed-clawback`:** solo la respuesta de `/transactions/4d4db33e…de88/operations`. Es un `clawback` **fallido** de 50 TESTUSB desde `GAQHKLXH…QXEG`: Horizon muestra el importe pedido con `transaction_successful: false`, y no genera ninguna retirada.
  - Queda fuera de la retención de RPC, así que no tiene evento SAC.
  - Se grabó con `RecordingClient` (lectura pública).

Los hashes completos están en las grabaciones y se verifican en `tests/stellar/test_adapter.py`.

**Contrapartes tipadas (incremento 3, capturadas el 2026-10-07 con `invaria stellar ingest --sac --page-limit 2 --sac-page-limit 200 --record`, solo lecturas públicas).**
- **`usdc-gdes54wt-cb-create`:** cuenta `GDES54WTI6UAVS6HQGRAFQTJ3KGC6YNO7HYO7ZZEYD6ANBPKPKA67H4L`, ledgers 5024596–5024599.
  - Cuatro `create_claimable_balance` de 0,5 USDC (evento CAP-67 `transfer` hacia un claimable balance, `ScAddress` tipo 3), con reclamantes la propia cuenta y otra.
  - Horizon no los lista entre los pagos. El adaptador lee `/claimable_balances/{id}/operations`; el resultado es `corroborated`.
- **`usdc-gapnrjhi-cb-claim`:** cuenta `GAPNRJHI635F3M6WAEUOSGIGKBBIDT4V6FDRK6KCETI3NNSIQHNZ6RU6`, ledger 5024599.
  - Reclama el balance `53cd2191…` que creó GDES54WT (`transfer` desde el balance).
- **`usdc-gdbxa45u-contract`:** cuenta `GDBXA45UBW2O3UH2RJOCOBXRGEMIP5745RQRINZZ2WHKECHHKKUWDOBH`, ledgers 5024520–5024524.
  - Seis movimientos de USDC con el contrato `CAYPAQDK…`, todos `invoke_host_function` listados por Horizon. No se infiere el propietario del contrato.
- **`usdc-feebump-payment`:** cuenta `GB3NNNLA2HVB2YKP3BXQIOT2UUZAZXZ25WC6IY542ZHAFHD56G2IS65Y`, ledger 5052280.
  - Un pago de 2,2367085 USDC dentro de un fee bump (exterior `415723e7…`, interior `57753597…`). Incluye `/transactions/{exterior|interior}` y sus operaciones.
  - RPC y Horizon nombran el hash exterior, también consultando por el interior.
- **`gdice-issuer-cb-clawback`:** se le añadieron **solo** los 2 intercambios nuevos (el historial en Horizon de sus dos balances). Los originales quedan byte a byte.
- Los legs con pool de `usdc-gcdkpp`, `usdc-gazy2f` y `usdc-gb4mm` se releen sin peticiones nuevas: el pool es ahora una parte tipada.

Las muestras de claimable balance y contrato se localizaron en las grabaciones de `usdc-gclcz` y `gdice` existentes (sus eventos ya estaban en `getEvents`). Las de fee bump, con `getTransactions` de RPC filtrando sobres de tipo fee bump.

## Regrabación de `getEvents` (2026-10-06)
El `endLedger` de RPC `getEvents` es exclusivo, y el adaptador ahora pide `end_ledger + 1`. Eso cambia la petición, así que en `usdc-gclcz`, `usdc-issuer`, `usdc-gb4mm` y `demoa-own` se regrabaron **solo** los intercambios `getEvents`, con su propio `captured_at`. Para hacerlo se volvió a capturar cada muestra en un directorio temporal:
- las páginas de Horizon y los ledgers salieron byte a byte idénticos a los grabados;
- `/assets` y la raíz `/` sí cambiaron, porque son estadísticas vivas, y se conservaron las originales.

El único evento nuevo en un último ledger es un `approve` de `usdc-issuer` (ledger 5015945), que no es un movimiento.

## Fixture SINTÉTICA de path payments (`synthetic/path-payments`)
**No es una grabación real.** La genera, de forma determinista, `synthetic/build_path_payments.py`:
- las cuentas, los hashes y el activo `SYNUSD` se derivan de etiquetas `invaria:synthetic:stellar:*` y no existen en ninguna red;
- cada cuerpo lleva `_synthetic` y cada metadato `synthetic`, y el target, `synthetic: true`;
- usa el formato de `ReplayClient`, para pasar por el mismo código que las muestras reales, pero bajo `https://horizon.synthetic.invalid` y `https://rpc.synthetic.invalid` y con ledgers 90 000 100–90 000 110, que no existen en testnet;
- el adaptador rechaza sus respuestas si el target no es sintético;
- imita los campos observados en Horizon y RPC, incluido el `endLedger` exclusivo.

Regenerarla es una decisión explícita, porque las pruebas fijan sus efectos.

## Fixture SINTÉTICA de fills del DEX (`synthetic/dex-fills`)
**No es una grabación real.** La genera `synthetic/build_dex_fills.py`, con las mismas garantías que la anterior (`.invalid`, ledgers 90 000 200–90 000 215, marcas `_synthetic`). Tiene 10 casos y un segundo target (`target-offer-owner.json`):
- fill por un path payment de un tercero, oferta propia como tomadora, dos ofertas en una operación, conflicto;
- operación DEX sin trade, tipo no previsto, operación no encontrada, página de efectos llena;
- llamada a contrato no listada, pago listado.

El caso del tipo no previsto usa `claim_claimable_balance` con partes cuenta solo para ejercitar esa rama: una reclamación real emite el claimable balance como parte.

## Fixture SINTÉTICA de clawback (`synthetic/clawbacks`)
**No es una grabación real.** La genera `synthetic/build_clawbacks.py`, con las mismas garantías (`.invalid`, ledgers 90 000 300–90 000 315, marcas `_synthetic`) y tres targets: el titular A, el titular B y el emisor. Cubre:
- clawback ejecutado y corroborado;
- conflicto de importes;
- transacción que Horizon no encuentra;
- clawback por llamada a contrato;
- operación Classic fallida con evento SAC;
- evento no exitoso;
- otro titular;
- otro activo.

## Fixture SINTÉTICA de muxed y memos (`synthetic/muxed`)
**No es una grabación real.** La genera, de forma determinista, `synthetic/build_muxed.py` (incremento 4). Un target, la cuenta vigilada. Cubre lo que las muestras reales no muestran:
- ids de sub-cuenta 0 y 2^64−1;
- emisor muxed con memo de texto;
- memo id 42 frente a sub-cuenta 42;
- receptor muxed con memo (el evento lleva la sub-cuenta);
- memos hash, return, texto vacío y texto no UTF-8;
- burn con memo (sin dato);
- contradicciones: otro id, otro memo, dato ausente;
- otro importe con dato coherente (texto o u64);
- llamada a contrato con u64 sin registro Classic.

## Fixture SINTÉTICA de contrapartes tipadas (`synthetic/typed-parties`)
**No es una grabación real.** La genera, de forma determinista, `synthetic/build_typed_parties.py` (incremento 3). Las cuentas, hashes, claimable balances, contratos, pools y activos se derivan de etiquetas `invaria:synthetic:stellar:*`. Cubre lo que las muestras reales no muestran:
- creación con importe en conflicto;
- operación fallida o de otro tipo;
- historial de balance no encontrado (participantes no resueltos);
- reclamación de una llamada fallida;
- reclamación de un balance de otro;
- clawback del balance visto por el emisor;
- clawback de un holder contrato;
- movimiento con un contrato listado y no listado;
- depósito en un pool;
- balance de otro activo.

Dos targets: la cuenta vigilada y el emisor.

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

## Límites
- Testnet se reinicia periódicamente. Las grabaciones permiten reproducir todo offline; la prueba real (`INVARIA_LIVE_TESTNET=1`) se omite si la historia ya no contiene la muestra.
- La cobertura es `provider_claimed`: es lo que Horizon y RPC devuelven, sin validación criptográfica de checkpoints ni fuente independiente.
